"""JSON property names cannot leak credentials or discard colliding evidence."""

from __future__ import annotations

import json

import pytest

from llmdv.http import redact
from llmdv.results.record import ResultRecord, ResultsWriter


@pytest.fixture(autouse=True)
def _clean_registry():
    redact.reset_secrets()
    yield
    redact.reset_secrets()


def test_nested_property_names_and_values_are_scrubbed_without_mutating_input():
    secret = "sk-json-property-secret"
    redact.install_secrets([secret])
    original = {"checks": [{f"prefix-{secret}": {"value": secret}}]}

    cleaned = redact.redact_value(original)

    assert cleaned == {"checks": [{f"prefix-{redact.SENTINEL}": {"value": redact.SENTINEL}}]}
    assert original == {"checks": [{f"prefix-{secret}": {"value": secret}}]}
    assert json.loads(json.dumps(cleaned)) == cleaned


def test_bearer_credentials_in_property_names_are_scrubbed_without_registration():
    cleaned = redact.redact_value({"Bearer unregistered-token": "observation"})
    assert cleaned == {f"Bearer {redact.SENTINEL}": "observation"}


def test_collisions_preserve_all_values_and_existing_numbered_names():
    secrets = ["sk-first-property", "sk-second-property", "sk-third-property"]
    redact.install_secrets(secrets)
    sentinel = redact.SENTINEL
    original = {
        secrets[0]: "first",
        secrets[1]: "second",
        f"{sentinel} [2]": "existing numbered name",
        sentinel: "existing sentinel",
        secrets[2]: ["third"],
    }

    cleaned = redact.redact_value(original)

    assert cleaned == {
        sentinel: "first",
        f"{sentinel} [3]": "second",
        f"{sentinel} [2]": "existing numbered name",
        f"{sentinel} [4]": "existing sentinel",
        f"{sentinel} [5]": ["third"],
    }
    assert redact.redact_value(cleaned) == cleaned
    assert len(cleaned) == len(original)


def test_results_writer_redacts_property_names_and_keeps_every_observation(tmp_path):
    secrets = ["sk-observed-property-one", "sk-observed-property-two"]
    redact.install_secrets(secrets)
    path = tmp_path / "results.jsonl"
    writer = ResultsWriter(path)
    try:
        writer.append(
            ResultRecord(
                run_id="example-run",
                nodeid="example-case",
                outcome="fail",
                profile="example",
                checks=[{"observed": {secrets[0]: "first", secrets[1]: "second"}}],
            )
        )
    finally:
        writer.close()

    text = path.read_text()
    assert all(secret not in text for secret in secrets)
    row = json.loads(text)
    assert row["checks"] == [
        {
            "observed": {
                redact.SENTINEL: "first",
                f"{redact.SENTINEL} [2]": "second",
            }
        }
    ]
