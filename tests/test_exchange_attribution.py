"""Offline evidence identity and correlation tests using a localhost HTTP server."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import httpx

from llmdv.http import redact
from llmdv.http.client import Exchange, RecordingClient, SdkRecorder
from llmdv.ingress.base import IngressAdapter
from llmdv.plugin import _correlation_headers, _write_exchanges
from llmdv.profile.schema import IngressKind


class _Server:
    def __enter__(self):
        seen = self.seen = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                self.rfile.read(length)
                seen.append({key.lower(): value for key, value in self.headers.items()})
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Server", "fake-server")
                self.send_header("Via", "fake-proxy")
                self.send_header("X-Forwarded-Proto", "https")
                self.end_headers()
                self.wfile.write(b'{"ok":true}')

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"
        return self

    def __exit__(self, *_):
        self.server.shutdown()
        self.server.server_close()


def test_raw_and_stream_requests_keep_case_identity_without_overwriting_probes():
    with _Server() as server:
        with RecordingClient(
            server.base_url,
            default_headers={"x-llmdv-run-id": "run-a", "x-llmdv-case-id": "case-a"},
        ) as client:
            client.post("/raw", json_body={}, headers={"x-llmdv-case-id": "probe-value"})
            client.stream("/stream", json_body={})

        with RecordingClient(
            server.base_url,
            default_headers={"x-llmdv-run-id": "run-a", "x-llmdv-case-id": "case-b"},
        ) as other:
            other.post("/other", json_body={})

        assert server.seen[0]["x-llmdv-run-id"] == "run-a"
        assert server.seen[0]["x-llmdv-case-id"] == "probe-value"
        assert server.seen[1]["x-llmdv-case-id"] == "case-a"
        assert server.seen[2]["x-llmdv-case-id"] == "case-b"


def test_plugin_correlation_identity_is_per_case():
    config = SimpleNamespace(_llmdv_run_id="run-d")
    first = SimpleNamespace(nodeid="node-a")
    second = SimpleNamespace(nodeid="node-b")
    first.get_closest_marker = lambda name: SimpleNamespace(args=("case-a",))
    second.get_closest_marker = lambda name: SimpleNamespace(args=("case-b",))
    assert _correlation_headers(config, first) == {
        "x-llmdv-run-id": "run-d",
        "x-llmdv-case-id": "case-a",
    }
    assert _correlation_headers(config, second)["x-llmdv-case-id"] == "case-b"


def test_sdk_recording_captures_correlation_and_response_identity():
    with _Server() as server:
        transport = httpx.HTTPTransport()
        client = httpx.Client(
            base_url=server.base_url,
            headers={"x-llmdv-run-id": "run-b", "x-llmdv-case-id": "case-b"},
            transport=transport,
        )
        recorder = SdkRecorder(client)
        response = client.post("/sdk", json={})
        recorder.close()

        assert response.status_code == 200
        assert server.seen[0]["x-llmdv-run-id"] == "run-b"
        assert server.seen[0]["x-llmdv-case-id"] == "case-b"
        assert recorder.last.response_body == {"ok": True}
        assert "fake-server" in recorder.last.response_headers["server"]


def test_exchange_metadata_is_attributable_and_identity_headers_are_redacted(tmp_path):
    path = tmp_path / "exchanges.jsonl"
    handle = path.open("w", encoding="utf-8")
    config = SimpleNamespace(_llmdv_exchanges=handle, _llmdv_run_id="run-c")
    item = SimpleNamespace(nodeid="test_node")
    marker = SimpleNamespace(args=("case-c",))
    exchanges = [
        Exchange(
            method="POST",
            url="http://unused",
            response_headers={"server": "secret-server", "via": "proxy"},
            status=200,
        )
    ]

    class Recorder:
        pass

    recorded = Recorder()
    recorded.exchanges = exchanges
    redact.install_secrets(["secret-server"])
    try:
        _write_exchanges(
            config,
            item,
            SimpleNamespace(id="edge", kind=IngressKind.LLMD_ROUTER),
            recorded,
            "chat_completions",
            marker,
        )
        handle.close()
        payload = json.loads(path.read_text())
        assert payload["ingress"] == "edge"
        assert payload["ingress_kind"] == "llmd_router"
        assert payload["case_id"] == "case-c"

        adapter = IngressAdapter()
        observed = adapter.served_by(exchanges[0]).to_dict()
        assert observed["extra"] == {"server": "secret-server", "via": "proxy"}
        assert "secret-server" not in path.read_text()
    finally:
        handle.close()
        redact.reset_secrets()


def test_absent_identity_headers_remain_unknown():
    observed = (
        IngressAdapter()
        .served_by(Exchange(method="POST", url="http://unused", status=200))
        .to_dict()
    )
    assert "extra" not in observed
