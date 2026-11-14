"""Shared result helpers for the evolution suite's offline tests."""

from tests.test_suites_against_fake import run_suites


def run_evolution(profile, output, select):
    code, records = run_suites(profile, output, "-k", select, "--llmdv-evidence", "all")
    by_case: dict[str, dict[str, dict]] = {}
    for record in records:
        if record.get("case_id"):
            by_case.setdefault(record["case_id"], {})[record.get("surface") or ""] = record
    return code, by_case


def failed(record, name):
    return any(check["name"] == name and not check["ok"] for check in record["checks"])
