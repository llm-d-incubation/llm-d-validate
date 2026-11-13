"""Record native client requests while preventing inference retries reaching the fleet.

This local relay buffers SSE for a compatibility probe; it does not measure client latency.
The fleet credential stays in the existing recording client, outside the child process.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from ..http.client import RecordingClient
from ..http.redact import SECRET_HEADERS


class ClientRelay:
    def __init__(
        self,
        client: RecordingClient,
        max_generations: int = 1,
        max_output_tokens: int = 256,
        max_body_bytes: int = 1024 * 1024,
    ) -> None:
        self.client = client
        self.max_generations = max_generations
        self.attempts = 0
        self.rejected = []
        self.generations = []
        lock = threading.Lock()
        relay = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_) -> None:
                pass

            def send(
                self,
                code: int,
                data: bytes,
                content_type: str = "application/json",
                extra_headers: dict[str, str] | None = None,
            ) -> None:
                self.send_response(code)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                for name, value in (extra_headers or {}).items():
                    self.send_header(name, value)
                self.end_headers()
                try:
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def deny(self, message: str) -> None:
                """Refuse a request the relay's own bounds do not allow, finally.

                `x-should-retry: false` matters more than the status here. The Anthropic SDK
                retries 408, 409, 429 and 5xx, and 409 was the status this used -- so a refusal
                written to STOP the client was asking it to try again, up to its retry limit,
                against bounds that will refuse it every time. The SDK checks that header before
                it looks at the status, so this ends the attempt.
                """
                relay.rejected.append(message)
                self.send(
                    409,
                    json.dumps(
                        {
                            "type": "error",
                            "error": {"type": "invalid_request_error", "message": message},
                        }
                    ).encode(),
                    extra_headers={"x-should-retry": "false"},
                )

            def do_POST(self):  # noqa -> None: N802 -- HTTP server interface
                if urlsplit(self.path).path != "/v1/messages":
                    return self.deny("probe accepts only Messages generations")
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= max_body_bytes:
                        raise ValueError("body limit")
                    body = json.loads(self.rfile.read(length))
                    if not isinstance(body, dict):
                        raise ValueError("body")
                    limit = body.get("max_tokens")
                    if type(limit) is not int or not 0 < limit <= max_output_tokens:
                        raise ValueError("output limit")
                except (ValueError, UnicodeDecodeError):
                    return self.deny("invalid or oversized client body")
                with lock:
                    relay.attempts += 1
                    if relay.attempts > relay.max_generations:
                        # A client repeats a generation after an error -- Claude Code falls back
                        # from streaming to non-streaming -- so the status that preceded the
                        # refusal is usually the finding. Name it where the client reports it.
                        after = ""
                        if relay.generations:
                            previous = relay.generations[-1][0]
                            status = previous.transport_error and "transport error"
                            after = f" after upstream {status or previous.status}"
                        return self.deny(
                            f"extra generation attempt{after}; retries are not forwarded"
                        )
                excluded = SECRET_HEADERS | {
                    "host",
                    "content-length",
                    "connection",
                    "transfer-encoding",
                    "accept-encoding",
                }
                headers = {
                    name: value
                    for name, value in self.headers.items()
                    if name.lower() not in excluded
                }
                if body.get("stream"):
                    exchange, transcript = client.stream(
                        self.path,
                        json_body=body,
                        headers=headers,
                        stop_event=("message_stop", "error"),
                    )
                    data = transcript.raw.encode()
                else:
                    exchange = client.post(self.path, json_body=body, headers=headers)
                    transcript = None
                    data = json.dumps(exchange.response_body).encode()
                relay.generations.append((exchange, transcript))
                if exchange.transport_error:
                    return self.send(
                        502,
                        b'{"error":{"type":"api_error","message":"upstream transport failure"}}',
                    )
                if exchange.status != 200:
                    data = json.dumps(exchange.response_body).encode()
                content_type = exchange.response_headers.get("content-type", "application/json")
                self.send(exchange.status, data, content_type)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def __enter__(self) -> ClientRelay:
        self.thread.start()
        return self

    def __exit__(self, *_) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
