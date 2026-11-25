"""Coverage modes share resolution across planning, execution, and unattended jobs."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from llmdv.cli import job_cmd
from llmdv.cli.main import build_parser
from llmdv.profile import inspection, loader, test_sets
from tests.fakes import FakeFleet, profile_yaml, run_llmdv
from tests.test_profile_extends import write

BASIC = "openai_chat.basic_non_streaming"
STREAM = "openai_chat.streaming_sse"


def profile_at(tmp_path, sets=None):
    raw = yaml.safe_load(profile_yaml("http://127.0.0.1:9"))
    raw["test_sets"] = sets
    return write(tmp_path / "profile.yaml", raw)


def plan(path, *flags):
    result = run_llmdv("plan", "--profile", str(path), "--json", *flags)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def verify(path, out, *flags):
    result = run_llmdv(
        "verify", "--profile", str(path), "--local-output", str(out), "--pod-logs", "off", *flags
    )
    outcome = json.loads((out / "outcome.json").read_text())
    return result, outcome


def test_modes_replace_default_combine_and_deduplicate(tmp_path):
    path = profile_at(tmp_path, {"default": [BASIC], "api": [BASIC, STREAM], "stream": [STREAM]})
    assert {r["case_id"] for r in plan(path)["cases"]} == {BASIC}
    both = plan(path, "--test-set", "api", "--test-set", "stream")
    assert {r["case_id"] for r in both["cases"]} == {BASIC, STREAM}
    assert len(both["cases"]) == 2
    assert both["coverage"]["mode"] == "named"
    assert [s["name"] for s in both["coverage"]["sets"]] == ["api", "stream"]
    all_sets = plan(path, "--all-test-sets")
    assert len(all_sets["cases"]) == 2
    assert all_sets["coverage"]["mode"] == "all-test-sets"
    assert plan(path, "--full-suite")["counts"]["total"] > 2
    narrowed = plan(path, "--test-set", "api", "--test-set", "stream", "-k", "test_chat_basic")
    assert [r["case_id"] for r in narrowed["cases"]] == [BASIC]
    assert narrowed["coverage"]["excluded_by_filters"] == 1


def test_yaml_wildcard_is_full_library_but_cli_wildcard_is_rejected(tmp_path):
    path = profile_at(tmp_path, {"everything": ["*"]})
    assert plan(path, "--all-test-sets")["counts"] == plan(path, "--full-suite")["counts"]
    result = run_llmdv("plan", "--profile", str(path), "--test-set", "*")
    assert result.returncode == 2
    assert "--all-test-sets" in result.stderr and "profile show" in result.stderr


@pytest.mark.parametrize(
    "flags", [(), ("--test-set", "missing"), ("--test-set", "default"), ("--all-test-sets",)]
)
def test_missing_scope_is_usage_before_requests(tmp_path, flags):
    path = profile_at(tmp_path)
    result, outcome = verify(path, tmp_path / "run", *flags)
    assert result.returncode == 2, result.stderr
    assert outcome["verification"]["class"] == "usage"
    assert not (tmp_path / "run" / "results.jsonl").exists()


@pytest.mark.parametrize(
    "flags",
    [
        ("--test-set", "smoke", "--full-suite"),
        ("--all-test-sets", "--full-suite"),
        ("--test-set", "smoke", "--all-test-sets"),
    ],
)
def test_modes_are_mutually_exclusive(tmp_path, flags):
    result = run_llmdv("plan", "--profile", str(profile_at(tmp_path)), *flags)
    assert result.returncode == 2


@pytest.mark.parametrize(
    "bad", [[], "tools.*", {"quick": []}, {"quick": [3]}, {"Bad Name": [BASIC]}, {"smoke": [BASIC]}]
)
def test_invalid_definitions_are_rejected(tmp_path, bad):
    with pytest.raises(loader.ProfileError, match="test_sets"):
        loader.load(profile_at(tmp_path, bad))


def test_only_chosen_patterns_are_checked(tmp_path):
    path = profile_at(tmp_path, {"default": ["typo.*"], "good": [BASIC], "bad": ["other-typo.*"]})
    assert plan(path, "--test-set", "good")["counts"]["run"] == 1
    result, outcome = verify(path, tmp_path / "run", "--test-set", "bad")
    assert result.returncode == 2
    assert "test_sets.bad" in result.stderr and "matches no case" in result.stderr
    assert outcome["verification"]["class"] == "usage"
    assert run_llmdv("plan", "--profile", str(path), "--all-test-sets").returncode == 2


def test_zero_after_filters_is_rejected_before_preflight(tmp_path):
    path = profile_at(tmp_path, {"default": [BASIC]})
    result, _ = verify(path, tmp_path / "run", "-k", "not_a_real_case_abcdef")
    assert result.returncode == 2
    assert "no tests" in result.stderr.lower() or "no cases" in result.stderr.lower()


def test_inheritance_export_and_digest(tmp_path):
    parent = profile_at(tmp_path, {"default": [BASIC], "api": [BASIC], "tools": ["tools.*"]})
    old_digest = loader.contract_digest(parent)
    child = write(
        tmp_path / "child.yaml",
        {"extends": parent.name, "name": "fake", "test_sets": {"api": [STREAM], "tools": None}},
    )
    profile, chain = loader.load_with_sources(child)
    assert profile.test_sets == {"default": (BASIC,), "api": (STREAM,)}
    assert loader.contract_digest(child) == old_digest
    text = inspection.explain_text(profile, chain, "test_sets.api")
    assert BASIC in text and STREAM in text
    exported = write(tmp_path / "export.yaml", yaml.safe_load(inspection.export_yaml(chain)))
    assert loader.load(exported).test_sets == profile.test_sets
    cleared = write(tmp_path / "cleared.yaml", {"extends": child.name, "test_sets": None})
    assert loader.load(cleared).test_sets == {}


def test_default_contract_warning_checks_coverage_not_literal_pattern(tmp_path):
    path = profile_at(tmp_path, {"default": [BASIC], "narrow": [BASIC]})
    warning = run_llmdv("plan", "--profile", str(path), "-q")
    assert "omits contract checks" in warning.stderr
    explicit = run_llmdv("plan", "--profile", str(path), "--test-set", "narrow", "-q")
    assert "omits contract checks" not in explicit.stderr
    all_cases = plan(path, "--full-suite")
    contract_ids = sorted(
        {r["case_id"] for r in all_cases["cases"] if r["case_id"].startswith("contract.")}
    )
    path = profile_at(tmp_path, {"default": contract_ids})
    explicit_ids = run_llmdv("plan", "--profile", str(path), "-q")
    assert explicit_ids.returncode == 0, explicit_ids.stderr
    assert "omits contract checks" not in explicit_ids.stderr


def test_smoke_is_reserved_small_and_all_sets_excludes_builtins(tmp_path):
    path = profile_at(tmp_path, {"api": [BASIC]})
    doc = plan(path, "--test-set", "smoke")
    assert len(doc["cases"]) == 9
    assert doc["coverage"]["sets"][0]["source"] == "built-in"
    assert plan(path, "--all-test-sets")["coverage"]["cases"] == [BASIC]
    assert not any("disruptive" in r["markers"] for r in doc["cases"])
    shown = run_llmdv("profile", "show", str(path), "--test-sets")
    assert "smoke (built-in)" in shown.stdout and "api (profile)" in shown.stdout


@pytest.mark.parametrize(
    "old,replacement", [("selection", "test_sets.default"), ("selections", "test_sets")]
)
def test_removed_yaml_keys_have_replacement_hints(tmp_path, old, replacement):
    path = profile_at(tmp_path)
    raw = yaml.safe_load(path.read_text())
    raw[old] = [BASIC]
    write(path, raw)
    result = run_llmdv("profile", "validate", str(path))
    assert result.returncode == 2 and replacement in result.stderr


@pytest.mark.parametrize(
    "old,replacement",
    [("--selection", "--test-set"), ("--all", "--full-suite"), ("--selections", "--test-sets")],
)
def test_removed_flags_have_replacement_hints(tmp_path, old, replacement):
    result = run_llmdv("plan", "--profile", str(profile_at(tmp_path)), old)
    assert result.returncode == 2 and replacement in result.stderr


def test_job_preserves_repeated_test_set_flags(tmp_path, monkeypatch):
    args, rest = build_parser().parse_known_args(
        ["job", "--profile", "p.yaml", "--test-set", "api", "--test-set", "tools", "-k", "stream"]
    )
    started = []

    class Child:
        def __init__(self, command):
            started.append(command)

        def wait(self, timeout=None):
            return 0

        def poll(self):
            return 0

    monkeypatch.setattr(job_cmd.subprocess, "Popen", Child)
    monkeypatch.setattr(job_cmd.signal, "signal", lambda *a: None)
    args.remote_output = None
    assert job_cmd._verify(args, tmp_path, rest) == 0
    command = started[0]
    assert command[-len(rest) :] == rest


@pytest.mark.parametrize(
    "path",
    sorted(
        p
        for p in Path("profiles/examples").rglob("*.yaml")
        if "parts" not in p.parts and not p.name.endswith(".local.yaml")
    ),
)
def test_every_example_has_a_working_default(path):
    assert loader.load(path).test_sets["default"]
    assert plan(path)["coverage"]["mode"] == "default"


@pytest.mark.parametrize(
    "path", sorted(p for p in Path("profiles/llm-d").glob("*/*.yaml") if p.parent.name != "parts")
)
def test_every_guide_default_plans(path):
    assert plan(path)["coverage"]["mode"] == "default"


@pytest.mark.parametrize("abort", [False, True])
def test_smoke_executes_existing_assertions(tmp_path, abort):
    with FakeFleet(abort_finish_reason=abort) as fleet:
        path = write(tmp_path / "profile.yaml", yaml.safe_load(profile_yaml(fleet.base_url)))
        result, outcome = verify(path, tmp_path / "run", "--test-set", "smoke")
    assert result.returncode == (1 if abort else 0), result.stderr
    assert outcome["coverage"]["mode"] == "named"
    assert outcome["coverage"]["sets"][0]["name"] == "smoke"


def test_coverage_recorded_before_unsatisfied_preflight(tmp_path):
    path = profile_at(tmp_path, {"api": [BASIC]})
    raw = yaml.safe_load(path.read_text())
    for ingress in raw["ingresses"]:
        ingress.pop("base_url")
        ingress["base_url_env"] = "LLMDV_TEST_UNSET_COVERAGE_URL"
    write(path, raw)
    result, outcome = verify(path, tmp_path / "run", "--test-set", "api")
    assert result.returncode == 3, result.stderr
    assert "selection" not in outcome
    coverage = outcome["coverage"]
    assert coverage["mode"] == "named"
    assert coverage["sets"] == [{"name": "api", "source": "profile", "cases": [BASIC]}]
    assert coverage["excluded_by_test_sets"] > 0


def test_replay_freezes_builtin_definition_and_preserves_filters(tmp_path, monkeypatch):
    from llmdv.cli import verify_cmd
    from llmdv.evidence import reproduce

    path = profile_at(tmp_path)
    flags = ["verify", "--profile", str(path), "--test-set", "smoke", "-k", "chat"]
    args = build_parser().parse_args(flags)
    monkeypatch.setattr(verify_cmd.sys, "argv", ["llmdv", *flags])
    out = tmp_path / "run"
    out.mkdir()
    expected = test_sets.BUILTIN["smoke"]
    verify_cmd._write_reproduce(args, loader.load(path), out, {})
    monkeypatch.setitem(test_sets.BUILTIN, "smoke", ("different.future.case",))
    replay = loader.load(out / "profile.replay.yaml")
    assert replay.test_sets["recorded-run"] == expected
    script = (out / "reproduce.sh").read_text()
    assert "--test-set recorded-run" in script
    assert "profile.replay.yaml" in script and "-k chat" in script
    assert "--test-set smoke" not in script
    assert reproduce.rewrite_argv(["llmdv", "--profile=old.yaml"], "new.yaml") == [
        "llmdv",
        "--profile",
        "new.yaml",
    ]
    assert reproduce.frozen_scope_argv(
        ["llmdv", "verify", "--test-set=smoke", "--", "-x"], "recorded-run"
    ) == ["llmdv", "verify", "--test-set", "recorded-run", "--", "-x"]


def test_all_skipped_banner_does_not_claim_pass(tmp_path, monkeypatch):
    from llmdv.cli import banner

    lines = []
    monkeypatch.setattr(banner, "emit", lines.append)
    banner.finish(tmp_path, [{"outcome": "skip", "skip_class": "undeclared"}], [], 0.1)
    text = "\n".join(lines)
    assert "NO CHECKS EXECUTED" in text and "PASSED" not in text


def test_comparison_accounts_for_additional_filter_exclusions():
    from llmdv.compare.diff import compare_window

    records = [{"case_id": name, "ingress": "engine", "outcome": "pass"} for name in ["a", "b"]]
    full = {"mode": "full-suite", "cases": []}
    narrowed = {**full, "excluded": [["engine", "b", ""]], "filters": {"keyword": "a"}}
    delta = compare_window([records], records[:1], [full], narrowed)
    assert delta.outside_coverage == ["b@engine"]
    assert delta.vanished == []


@pytest.mark.parametrize("command", ["plan", "verify", "job"])
@pytest.mark.parametrize("flag", ["--llmdv-full-suite", "--llmdv-test-set=smoke"])
def test_internal_scope_flags_cannot_override_recorded_coverage(command, flag):
    result = run_llmdv(command, "--profile", "unused.yaml", flag)
    assert result.returncode == 2
    assert "pytest plugin option; use --" in result.stderr
