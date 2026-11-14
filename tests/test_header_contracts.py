import json

import pytest
import yaml

from llmdv.http.client import RecordingClient
from tests.fakes import FakeFleet, run_llmdv
from tests.test_suites_against_fake import run_suites, write_profile


@pytest.mark.parametrize("scheme,header", [("bearer", "Authorization"), ("x-api-key", "x-api-key")])
@pytest.mark.parametrize("defect", [False, True])
def test_missing_auth_cannot_hide_behind_a_valid_key(tmp_path, scheme, header, defect):
    secret = "llmdv-test-header-secret"
    with FakeFleet(
        auth_header=(header, f"Bearer {secret}" if scheme == "bearer" else secret),
        accept_missing_auth=defect,
    ) as fleet:
        path = write_profile(tmp_path, fleet)
        raw = yaml.safe_load(path.read_text())
        raw["ingresses"][0]["auth"] = {"type": scheme, "key_env": "LLMDV_HEADER_KEY"}
        path.write_text(yaml.safe_dump(raw))
        run = tmp_path / "run"
        result = run_llmdv(
            "verify",
            "--profile",
            str(path),
            "--local-output",
            str(run),
            "--pod-logs",
            "off",
            "--skip-preflight",
            "-k",
            "test_required_authentication",
            env={"LLMDV_HEADER_KEY": secret},
        )
    assert result.returncode == (1 if defect else 0), result.stdout + result.stderr
    records = [json.loads(line) for line in (run / "results.jsonl").read_text().splitlines()]
    assert all(r["outcome"] == ("fail" if defect else "pass") for r in records)
    assert secret not in (run / "exchanges.jsonl").read_text()


@pytest.mark.parametrize(
    "declared,ignores,ok",
    [(400, False, True), (400, True, False), (200, True, True), (200, False, False)],
)
def test_version_rule_is_measured_not_assumed(tmp_path, declared, ignores, ok):
    with FakeFleet(ignore_api_version=ignores) as fleet:
        path = write_profile(tmp_path, fleet)
        raw = yaml.safe_load(path.read_text())
        raw["ingresses"][0].setdefault("error_status", {})["invalid_api_version"] = [declared]
        path.write_text(yaml.safe_dump(raw))
        code, records = run_suites(path, tmp_path / "run", "-k", "test_messages_version_header")
    assert code == (0 if ok else 1), records
    assert all(r["outcome"] == ("pass" if ok else "fail") for r in records)


@pytest.mark.parametrize("value", [None, "", "20"])
def test_promised_rate_header_requires_nonempty_response_value(tmp_path, value):
    header = "x-ratelimit-remaining-requests"
    with FakeFleet(extra_response_headers={} if value is None else {header: value}) as fleet:
        path = write_profile(tmp_path, fleet)
        raw = yaml.safe_load(path.read_text())
        raw["headers"] = {header: {"fake": "promised"}}
        path.write_text(yaml.safe_dump(raw))
        code, records = run_suites(path, tmp_path / "run", "-k", "test_header_disposition")
        assert all(header not in {key.lower() for key in headers} for _, _, headers in fleet.seen)
    assert code == (0 if value else 1), records


def test_omission_is_case_insensitive_and_does_not_mutate_defaults():
    with (
        FakeFleet() as fleet,
        RecordingClient(
            fleet.base_url, default_headers={"AUTHORIZATION": "Bearer example"}
        ) as client,
    ):
        first = client.get("/health", omit_headers=("authorization",))
        second = client.get("/health")
    assert "authorization" not in {name.lower() for name in first.request_headers}
    assert "authorization" in {name.lower() for name in second.request_headers}
