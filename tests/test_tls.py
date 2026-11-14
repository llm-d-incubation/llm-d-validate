"""`ingresses[].tls.verify`: an untrusted certificate fails preflight unless verification is off."""

from __future__ import annotations

import shutil
import subprocess

import pytest

from llmdv import preflight
from llmdv.profile import loader
from tests.fakes import FakeFleet, profile_yaml
from tests.test_suites_against_fake import outcomes, run_suites

pytestmark = pytest.mark.skipif(shutil.which("openssl") is None, reason="needs openssl")


@pytest.fixture
def self_signed(tmp_path):
    """A certificate no trust store knows, like a cluster's own ingress CA."""
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    subprocess.run(  # noqa: S603, S607 - fixed command
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
         "-keyout", str(key), "-out", str(cert), "-subj", "/CN=127.0.0.1",
         "-addext", "subjectAltName=IP:127.0.0.1"],
        check=True, capture_output=True,
    )  # fmt: skip
    return cert, key


def write(tmp_path, fleet, verify):
    text = profile_yaml(fleet.base_url, fleet.model)
    if not verify:
        text = text.replace(
            "    health_path: /health\n", "    health_path: /health\n    tls: {verify: false}\n"
        )
    path = tmp_path / "tls.yaml"
    path.write_text(text)
    return path


def test_an_untrusted_certificate_fails_preflight_by_default(tmp_path, self_signed):
    with FakeFleet(tls_cert=self_signed) as fleet:
        report = preflight.run(loader.load(write(tmp_path, fleet, verify=True)))
    assert not report.ok
    assert report.problems[0].kind == "reachability"


def test_verify_false_reaches_the_fleet_and_warns(tmp_path, self_signed):
    with FakeFleet(tls_cert=self_signed) as fleet:
        profile = write(tmp_path, fleet, verify=False)
        assert preflight.run(loader.load(profile)).ok
        # Both the raw client and the pinned SDK honour it.
        code, records = run_suites(profile, tmp_path / "run", "-k", "test_chat_basic")

    assert code == 0, outcomes(records)
    assert outcomes(records) == {
        "openai_chat.basic_non_streaming": "pass",
        "openai_chat.basic_parses_with_openai_sdk": "pass",
    }
    warnings = [d for d in loader.validate(loader.load(profile)) if d.path.endswith("tls.verify")]
    assert [d.level for d in warnings] == ["warning"]
