"""Operator-run Chat-shaped backend for an isolated header-transformation route.

Only explicitly allowlisted non-credential headers are returned. llmdv never deploys this
server or redirects a fleet's routes; operators configure an isolated route themselves.
"""

from __future__ import annotations

import argparse
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from ..http.redact import SECRET_HEADERS


def make_server(
    bind: str, port: int, headers: list[str], model: str = "llmdv-header-probe"
) -> ThreadingHTTPServer:
    names = sorted(set(name.lower() for name in headers))
    if not names or any(
        name in SECRET_HEADERS
        or name == "x-llmdv-probe-id"
        or re.fullmatch(r"[a-z0-9][a-z0-9-]*", name) is None
        for name in names
    ):
        raise ValueError("allowlist requires non-credential HTTP header names")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_) -> None:
            pass  # Never log request lines or incoming headers.

        def send(self, code: int, body: dict[str, Any]) -> None:
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):  # noqa -> None: N802 -- HTTP server interface
            if self.path == "/health":
                self.send(200, {"status": "ok"})
            elif self.path == "/v1/models":
                self.send(200, {"object": "list", "data": [{"id": model, "object": "model"}]})
            else:
                self.send(404, {"error": {"type": "not_found", "message": "unknown route"}})

        def do_POST(self):  # noqa -> None: N802 -- HTTP server interface
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 65536:
                    raise ValueError("body limit")
                body = json.loads(self.rfile.read(length))
                if not isinstance(body, dict) or body.get("model") != model:
                    raise ValueError("model")
            except (ValueError, UnicodeDecodeError):
                self.send(
                    400,
                    {
                        "error": {
                            "type": "invalid_request_error",
                            "message": "invalid probe request",
                        }
                    },
                )
                return
            if self.path != "/v1/chat/completions":
                self.send(404, {"error": {"type": "not_found", "message": "unknown route"}})
                return
            probe_id = self.headers.get("x-llmdv-probe-id", "")
            if re.fullmatch(r"[a-f0-9]{32}", probe_id) is None:
                self.send(
                    400, {"error": {"type": "invalid_request_error", "message": "invalid probe ID"}}
                )
                return
            self.send(
                200,
                {
                    "object": "chat.completion",
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "ok"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                    "llmdv_header_probe": {
                        "instrument": "llmdv.header_echo/1",
                        "probe_id": probe_id,
                        "allowlist": names,
                        "headers": {
                            name: self.headers[name] for name in names if name in self.headers
                        },
                    },
                },
            )

    return ThreadingHTTPServer((bind, port), Handler)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--model", default="llmdv-header-probe")
    parser.add_argument("--allow-header", action="append", required=True)
    args = parser.parse_args()
    try:
        server = make_server(args.bind, args.port, args.allow_header, args.model)
    except ValueError as exc:
        parser.error(str(exc))
    with server:
        server.serve_forever()


if __name__ == "__main__":
    main()
