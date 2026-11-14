"""A recording HTTP client.

Raw httpx is the primary instrument, not an SDK. Most of what has to be proven here is
invisible through a typed client because the SDK normalises it away: malformed JSON, wrong
field types, wrong HTTP methods, unknown paths, header manipulation, mid-stream disconnects.
The SDKs appear only where SDK compatibility is itself the requirement.

Every exchange is recorded, redacted at capture time rather than at write time, so there is
no window in which an unredacted body sits in memory waiting to be serialised into a bundle.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..log.setup import get_logger
from . import redact, sse

log = get_logger("http")


@dataclass
class Exchange:
    """One request/response pair, already redacted, ready to serialise as evidence."""

    method: str
    url: str
    request_headers: dict[str, str] = field(default_factory=dict)
    request_body: Any = None
    status: int | None = None
    response_headers: dict[str, str] = field(default_factory=dict)
    response_body: Any = None
    response_text: str | None = None
    elapsed_s: float = 0.0
    transport_error: str | None = None
    # Streaming only: whether the stored transcript is partial, and how big the real one was.
    transcript_truncated: bool = False
    transcript_bytes: int | None = None
    transcript_full: str | None = field(default=None, repr=False)
    transcript_ref: dict[str, Any] | None = None
    client_closed: bool = False
    # Opt-in observations: seconds until a stream's response headers arrived, and the
    # negotiated connection (HTTP version, TLS version, ALPN, local address).
    headers_s: float | None = None
    connection: dict[str, Any] | None = None

    @property
    def ok(self) -> bool:
        return self.status is not None and 200 <= self.status < 300

    @property
    def json_body(self) -> Any:
        """Parsed JSON body, or None when the body was not JSON.

        Distinguishing "not JSON" from "JSON null" matters for the malformed-response cases,
        so a non-JSON body keeps its text in `response_text` instead.
        """
        return self.response_body

    def to_dict(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "url": self.url,
            "request_headers": self.request_headers,
            "request_body": self.request_body,
            "status": self.status,
            "response_headers": self.response_headers,
            "response_body": self.response_body
            if self.response_body is not None
            else self.response_text,
            "elapsed_s": round(self.elapsed_s, 4),
            "transport_error": self.transport_error,
            **({"client_closed": True} if self.client_closed else {}),
            **({"headers_s": round(self.headers_s, 4)} if self.headers_s is not None else {}),
            **({"connection": self.connection} if self.connection is not None else {}),
            **(
                {"transcript_truncated": True, "transcript_bytes": self.transcript_bytes}
                if self.transcript_truncated
                else {}
            ),
            **({"transcript": self.transcript_ref} if self.transcript_ref is not None else {}),
        }


class RecordingClient:
    """httpx wrapper that keeps a redacted record of everything it sends.

    `trust_env=False` is deliberate and load-bearing. A profile declaring `proxy: bypass`
    means localhost traffic must not traverse the VPN proxy, and httpx would otherwise pick
    HTTP_PROXY up from the environment. An unnoticed proxy hop produces plausible, wrong
    results, a confounder that is easy to miss.
    """

    def __init__(
        self,
        base_url: str,
        *,
        default_headers: dict[str, str] | None = None,
        connect_timeout: float = 5.0,
        request_timeout: float = 600.0,
        trust_env: bool = False,
        verify: bool = True,
        stream_idle: float = 60.0,
        transcript_cap: int = sse.DEFAULT_TRANSCRIPT_CAP,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.default_headers = dict(default_headers or {})
        self.stream_idle = stream_idle
        self.transcript_cap = transcript_cap
        self.exchanges: list[Exchange] = []
        self._client = httpx.Client(
            timeout=httpx.Timeout(request_timeout, connect=connect_timeout),
            trust_env=trust_env,
            verify=verify,
            follow_redirects=False,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> RecordingClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @property
    def last(self) -> Exchange | None:
        return self.exchanges[-1] if self.exchanges else None

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        raw_body: str | bytes | None = None,
        headers: dict[str, str] | None = None,
        content_type: str | None = "application/json",
        omit_headers: Collection[str] = (),
        chunked: bool = False,
        record_body: Any = None,
        inspect_connection: bool = False,
        timeout: float | None = None,
    ) -> Exchange:
        """Send one request and record it.

        `raw_body` bypasses JSON encoding so a deliberately malformed payload can be sent
        verbatim -- a json-encoding client cannot express "invalid JSON". `chunked` sends the
        encoded body with Transfer-Encoding: chunked and no Content-Length. `record_body`
        replaces what evidence records for a body that is not readable text, such as gzip.
        `inspect_connection` records the negotiated connection, and `timeout` replaces the
        ingress's request timeout for this one request.
        """
        url = f"{self.base_url}{path}"
        sent = httpx.Headers(self.default_headers)
        sent.update(headers or {})
        for name in omit_headers:
            sent.pop(name, None)
        if content_type and (json_body is not None or raw_body is not None):
            sent.setdefault("Content-Type", content_type)

        exchange = Exchange(
            method=method.upper(),
            url=url,
            request_headers=redact.redact_headers(sent),
            request_body=json_body if raw_body is None else _preview(raw_body),
        )
        if record_body is not None:
            exchange.request_body = record_body

        content: Any = raw_body
        if chunked:
            encoded = json.dumps(json_body).encode() if raw_body is None else _as_bytes(raw_body)
            sent.setdefault("Content-Type", content_type or "application/json")
            json_body, content = None, _pieces(encoded)
        extra: dict[str, Any] = {} if timeout is None else {"timeout": timeout}
        started = time.monotonic()
        try:
            response = self._client.request(
                method.upper(),
                url,
                headers=sent,
                json=json_body if content is None else None,
                content=content,
                **extra,
            )
        except httpx.HTTPError as exc:
            exchange.elapsed_s = time.monotonic() - started
            exchange.transport_error = f"{type(exc).__name__}: {exc}"
            log.debug("%s %s -> transport error: %s", method.upper(), url, exchange.transport_error)
            self.exchanges.append(exchange)
            return exchange

        exchange.elapsed_s = time.monotonic() - started
        exchange.status = response.status_code
        exchange.response_headers = redact.redact_headers(dict(response.headers))
        if inspect_connection:
            exchange.connection = connection_info(response)
        text = response.text
        try:
            exchange.response_body = json.loads(text) if text else None
        except ValueError:
            exchange.response_body = None
            exchange.response_text = redact.scrub(text[:8000])
        log.debug("%s %s -> %s in %.3fs", exchange.method, url, exchange.status, exchange.elapsed_s)
        self.exchanges.append(exchange)
        return exchange

    def stream(
        self,
        path: str,
        *,
        json_body: Any = None,
        headers: dict[str, str] | None = None,
        stop_event: str | Collection[str] | None = None,
        stop_when: Callable[[sse.Event], bool] | None = None,
        max_events: int | None = None,
        drain: bool = False,
        framing: bool = False,
        close_on_headers: bool = False,
        idle_timeout: float | None = None,
    ) -> tuple[Exchange, sse.Transcript]:
        """POST and read the response as server-sent events.

        Returns the recorded exchange and the transcript together, so a case can assert on the
        frames while the evidence layer gets the same object. The profile's
        `timeouts.stream_idle` bounds each read, including a connection that stops sending.

        `drain` keeps reading after the terminal event until EOF, so a case can see frames a
        server sends after its own terminal event. `framing` splits the raw bytes itself and
        records line terminators and UTF-8 validity, which a decoded line reader hides.
        `close_on_headers` closes a successful stream as soon as its headers arrive, before
        any event: during prefill on a long prompt. `idle_timeout` replaces `stream_idle` for this
        one stream.
        """
        url = f"{self.base_url}{path}"
        sent = httpx.Headers(self.default_headers)
        sent.update(headers or {})
        sent.setdefault("Content-Type", "application/json")
        sent.setdefault("Accept", "text/event-stream")

        exchange = Exchange(
            method="POST",
            url=url,
            request_headers=redact.redact_headers(sent),
            request_body=json_body,
        )
        transcript = sse.Transcript()
        if isinstance(json_body, dict):
            transcript.requested_model = json_body.get("model")
            transcript.requested_usage = (json_body.get("stream_options") or {}).get(
                "include_usage"
            ) is True
        started = time.monotonic()
        idle = self.stream_idle if idle_timeout is None else idle_timeout
        base = self._client.timeout
        timeout = httpx.Timeout(connect=base.connect, read=idle, write=base.write, pool=base.pool)
        try:
            with self._client.stream(
                "POST",
                url,
                headers=sent,
                json=json_body,
                timeout=timeout,
            ) as response:
                exchange.headers_s = time.monotonic() - started
                exchange.status = response.status_code
                exchange.response_headers = redact.redact_headers(dict(response.headers))
                if close_on_headers and response.status_code < 400:
                    transcript.client_closed = True
                elif response.status_code >= 400:
                    # An error response is a normal body, not a stream; read it so the failure
                    # is as legible as a non-streaming one.
                    response.read()
                    text = response.text
                    try:
                        exchange.response_body = json.loads(text) if text else None
                    except ValueError:
                        exchange.response_text = redact.scrub(text[:8000])
                else:
                    lines = (
                        sse.split_lines(response.iter_bytes(), transcript.framing)
                        if framing
                        else response.iter_lines()
                    )
                    sse.read(
                        lines,
                        idle_timeout=idle,
                        stop_event=stop_event,
                        transcript=transcript,
                        stop_when=stop_when,
                        max_events=max_events,
                        drain=drain,
                    )
        except sse.StreamError as exc:
            exchange.transport_error = f"{type(exc).__name__}: {exc}"
        except httpx.HTTPError as exc:
            if drain and transcript.terminal_seen:
                # The stream itself completed; only the read past its terminal event failed.
                transcript.framing["drain_error"] = f"{type(exc).__name__}: {exc}"
            else:
                exchange.transport_error = f"{type(exc).__name__}: {exc}"

        exchange.elapsed_s = time.monotonic() - started
        exchange.client_closed = transcript.client_closed
        if transcript.raw:
            scrubbed = redact.scrub(transcript.raw)
            body, truncated = sse.clamp(scrubbed, self.transcript_cap)
            exchange.response_text = body
            exchange.transcript_truncated = truncated
            exchange.transcript_bytes = transcript.bytes_received
            if truncated:
                exchange.transcript_full = scrubbed
        log.debug(
            "POST %s -> %s, %d event(s) in %.3fs",
            url,
            exchange.status,
            len(transcript.events),
            exchange.elapsed_s,
        )
        self.exchanges.append(exchange)
        return exchange, transcript

    def get(self, path: str, **kw: Any) -> Exchange:
        return self.request("GET", path, content_type=None, **kw)

    def post(self, path: str, **kw: Any) -> Exchange:
        return self.request("POST", path, **kw)


def _as_bytes(body: str | bytes) -> bytes:
    return body.encode("utf-8") if isinstance(body, str) else body


def _pieces(body: bytes, size: int = 64) -> Any:
    """A body as a generator, which httpx sends with Transfer-Encoding: chunked."""

    def generate():
        for start in range(0, len(body), size):
            yield body[start : start + size]

    return generate()


def connection_info(response: Any) -> dict[str, Any]:
    """The negotiated HTTP version, TLS version, ALPN protocol and local address."""
    extensions = getattr(response, "extensions", {}) or {}
    version = extensions.get("http_version")
    info: dict[str, Any] = {
        "http_version": version.decode() if isinstance(version, bytes) else version
    }
    stream = extensions.get("network_stream")
    if stream is not None:
        ssl_object = stream.get_extra_info("ssl_object")
        client_addr = stream.get_extra_info("client_addr")
        info["local_addr"] = list(client_addr) if client_addr else None
        if ssl_object is not None:
            info["tls_version"] = ssl_object.version()
            info["alpn"] = ssl_object.selected_alpn_protocol()
    return info


def _preview(body: str | bytes) -> str:
    text = body.decode("utf-8", "replace") if isinstance(body, bytes) else body
    return redact.scrub(text[:4000])


class SdkRecorder:
    """Records exchanges a third-party SDK makes, through httpx event hooks.

    The pinned OpenAI and Anthropic SDKs issue their own requests, so a case that goes through
    one would otherwise produce no exchange record at all. Handing the SDK a client with hooks
    attached captures evidence without giving up the SDK's own serialisation, which is the
    thing those cases exist to exercise.

    Duck-types `RecordingClient` for `exchanges` and `last`, so the reporter treats both alike.
    Hooks accept any httpx-compatible request and response, including httpx2 objects.
    """

    def __init__(self, client: Any) -> None:
        self.exchanges: list[Exchange] = []
        # `response.elapsed` is not populated inside a response hook, so timing is taken from a
        # request hook instead. Recording 0.000s would be worse than recording nothing: it reads
        # as an instant round trip in evidence someone is relying on.
        self._started: dict[int, float] = {}
        self.client = client
        self.client.event_hooks["request"].append(self._on_request)
        self.client.event_hooks["response"].append(self._on_response)

    @property
    def last(self) -> Exchange | None:
        return self.exchanges[-1] if self.exchanges else None

    def close(self) -> None:
        self.client.close()

    def _on_request(self, request: Any) -> None:
        self._started[id(request)] = time.monotonic()

    def _on_response(self, response: Any) -> None:
        request = response.request
        started = self._started.pop(id(request), None)
        exchange = Exchange(
            method=request.method,
            url=str(request.url),
            request_headers=redact.redact_headers(dict(request.headers)),
            request_body=_decode(getattr(request, "content", b"")),
            status=response.status_code,
            response_headers=redact.redact_headers(dict(response.headers)),
            elapsed_s=(time.monotonic() - started) if started is not None else 0.0,
        )
        try:
            # A streamed response must not be drained here: reading it would consume the body
            # the SDK is about to parse. Those cases are covered by RecordingClient instead.
            content_type = response.headers.get("content-type", "")
            if not response.is_stream_consumed and content_type.startswith("application/json"):
                response.read()
                exchange.response_body = json.loads(response.text) if response.text else None
        except Exception as exc:  # noqa: BLE001 -- evidence capture must never fail a case
            log.debug("could not capture SDK response body: %s", exc)
        self.exchanges.append(exchange)


def _decode(body: bytes) -> Any:
    if not body:
        return None
    try:
        return json.loads(body)
    except ValueError:
        return redact.scrub(body.decode("utf-8", "replace")[:4000])
