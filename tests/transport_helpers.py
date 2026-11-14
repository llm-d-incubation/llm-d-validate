"""Shared helpers for exercising transport cases against the fake fleet."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from tests.fakes import FakeFleet, profile_yaml
from tests.test_suites_against_fake import run_suites


def declare(tmp_path: Path, fleet: FakeFleet, transport=None, error_status=None, **kw) -> Path:
    raw = yaml.safe_load(profile_yaml(fleet.base_url, fleet.model, **kw))
    ingress = raw["ingresses"][0]
    if transport is not None:
        ingress["transport"] = transport
    ingress["error_status"].update(error_status or {})
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "fake.yaml"
    path.write_text(yaml.safe_dump(raw))
    return path


def run(tmp_path: Path, fleet: FakeFleet, select: str, **declared):
    profile = declare(tmp_path, fleet, **declared)
    code, records = run_suites(profile, tmp_path / "run", "-k", select, "--llmdv-evidence", "all")
    by_case: dict[str, list[dict]] = {}
    for record in records:
        if record.get("case_id"):
            by_case.setdefault(record["case_id"], []).append(record)
    return code, by_case


def outcomes(by_case: dict[str, list[dict]], case_id: str) -> set[str]:
    return {record["outcome"] for record in by_case.get(case_id, [])}


def exchanges(tmp_path: Path) -> list[dict]:
    lines = (tmp_path / "run" / "exchanges.jsonl").read_text().splitlines()
    return [json.loads(line) for line in lines]
