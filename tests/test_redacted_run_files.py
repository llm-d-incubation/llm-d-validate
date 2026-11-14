"""No run file carries the gateway key, even when the gateway echoes it back.

A gateway that rejects a key often quotes it in the error: `{"error": {"message": "invalid key
sk-..."}}`. Headers and plain-text bodies were redacted at capture, but a JSON body was stored
as parsed, and assertion text and result reasons were written as-is, so the echoed key reached
exchanges.jsonl, response.json, exchanges.json, assertion.txt, results.jsonl and outcome.json --
all of which a run publishes.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler

import yaml

from tests.fakes import profile_yaml, run_llmdv
from tests.transport_fakes import QuietServer

KEY = "sk-echoed-key-0123456789abcdef"


class EchoingGateway:
    """Healthy on GET; every POST is a 401 that quotes the bearer token it was sent."""

    def __enter__(self):
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # noqa: A002 - silence the access log
                pass

            def _send(self, status, body):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self._send(200, {"status": "ok", "data": [{"id": "m", "object": "model"}]})

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
                token = (self.headers.get("Authorization") or "").removeprefix("Bearer ")
                self._send(
                    401,
                    {
                        "error": {
                            "message": f"Authentication Error, invalid key {token}",
                            "type": "auth_error",
                            "api_key": token,
                        }
                    },
                )

        self.server = QuietServer(("127.0.0.1", 0), Handler)
        host, port = self.server.server_address[:2]
        self.base_url = f"http://{host}:{port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *_):
        self.server.shutdown()
        self.server.server_close()


def test_an_echoed_key_reaches_no_run_file(tmp_path):
    with EchoingGateway() as gateway:
        profile = yaml.safe_load(profile_yaml(gateway.base_url, "m"))
        profile["ingresses"][0]["auth"] = {"type": "bearer", "key_env": "LLMDV_ECHO_KEY"}
        path = tmp_path / "echo.yaml"
        path.write_text(yaml.safe_dump(profile))
        run_dir = tmp_path / "run"
        result = run_llmdv(
            "verify", "--profile", str(path), "--local-output", str(run_dir),
            "--pod-logs", "off", "--evidence", "all", "-k", "test_chat_basic and not sdk",
            env={"LLMDV_ECHO_KEY": KEY},
        )  # fmt: skip

    # The case ran and failed on the 401, so every evidence file was written.
    assert result.returncode == 1, result.stdout + result.stderr
    files = [p for p in run_dir.rglob("*") if p.is_file()]
    assert any(p.name == "response.json" for p in files)
    assert any(p.name == "assertion.txt" for p in files)

    leaked = [str(p.relative_to(run_dir)) for p in files if KEY in p.read_text(errors="replace")]
    assert leaked == [], f"the echoed key is in: {leaked}"
    assert KEY not in result.stdout + result.stderr

    # Redacting inside JSON must leave every JSON file parseable.
    for p in files:
        if p.suffix == ".json":
            json.loads(p.read_text())
        elif p.suffix == ".jsonl":
            for line in p.read_text().splitlines():
                json.loads(line)
