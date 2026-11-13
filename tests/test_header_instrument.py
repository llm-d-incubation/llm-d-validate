import json
import threading

import pytest
import yaml

from llmdv.http.client import RecordingClient
from llmdv.instruments.header_echo import make_server
from tests.fakes import FakeFleet
from tests.test_suites_against_fake import run_suites, write_profile


@pytest.mark.parametrize("disposition", ["forwarded", "stripped", "rewritten"])
@pytest.mark.parametrize("defect", [None, "wrong", "stale", "unconfigured"])
def test_backend_contract(tmp_path, disposition, defect):
    with FakeFleet(header_probe_disposition=disposition, header_probe_defect=defect) as fleet:
        path = write_profile(tmp_path, fleet)
        raw = yaml.safe_load(path.read_text())
        raw["capabilities"] = {
            "header_probe": True,
            "header_rewrites": {"x-llmdv-example": "rewritten"},
        }
        raw["headers"] = {"x-llmdv-example": {"fake": disposition}}
        path.write_text(yaml.safe_dump(raw))
        code, records = run_suites(path, tmp_path / "run", "-k", "test_header_backend_disposition")
    assert code == (0 if defect is None else 1), records
    assert records[0]["outcome"] == ("pass" if defect is None else "fail"), records


def test_echo_backend_reports_only_allowlisted_headers():
    server = make_server("127.0.0.1", 0, ["x-llmdv-example"])
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with RecordingClient(f"http://127.0.0.1:{server.server_port}") as client:
            response = client.post(
                "/v1/chat/completions",
                json_body={"model": "llmdv-header-probe"},
                headers={
                    "x-llmdv-probe-id": "a" * 32,
                    "x-llmdv-example": "visible",
                    "Authorization": "Bearer private-value",
                    "Cookie": "private-cookie",
                    "x-unlisted": "private-other",
                },
            )
        assert response.status == 200
        payload = response.response_body
        assert payload["llmdv_header_probe"]["headers"] == {"x-llmdv-example": "visible"}
        assert "private" not in json.dumps(payload)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize(
    "name", ["Authorization", "cookie", "x-api-key", "x-llmdv-probe-id", "bad:name"]
)
def test_credentials_cannot_be_allowlisted(name):
    with pytest.raises(ValueError, match="non-credential"):
        make_server("127.0.0.1", 0, [name])
