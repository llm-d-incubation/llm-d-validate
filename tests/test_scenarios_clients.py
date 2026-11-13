"""Structured-output client cases, each proven to fail on the defect it targets."""

import json

import pytest
import yaml

from tests.fakes import MODEL, FakeFleet
from tests.test_suites_against_fake import run_suites, write_profile

CASE = "scenarios.structured_client"


def run_clients(
    tmp_path, *, efforts=("none",), quiet=(), select="test_structured_client", **fleet_kw
):
    with FakeFleet(**fleet_kw) as fleet:
        path = write_profile(tmp_path, fleet, responses=False, structured_output=True)
        raw = yaml.safe_load(path.read_text())
        raw["ingresses"][0]["surfaces"] = ["chat_completions"]
        raw["capabilities"]["chat_reasoning_efforts"] = list(efforts)
        raw["capabilities"]["chat_quiet_efforts"] = list(quiet)
        path.write_text(yaml.safe_dump(raw))
        code, records = run_suites(path, tmp_path / "run", "-k", select)
    return code, {r["variant"].rpartition("/")[2]: r for r in records if r["case_id"] == CASE}


def test_both_clients_pass_on_a_healthy_fake(tmp_path):
    code, records = run_clients(tmp_path)
    assert code == 0, records
    assert {k: r["outcome"] for k, r in records.items()} == {"judge": "pass", "triage": "pass"}
    sent = [
        json.loads(line)["request_body"]
        for line in (tmp_path / "run" / "exchanges.jsonl").read_text().splitlines()
    ]
    judge, triage = sorted(sent, key=lambda body: body["max_tokens"])
    assert judge["max_tokens"] == 16 and "temperature" not in judge
    assert triage["temperature"] == 0 and triage["messages"][0]["role"] == "system"
    assert all(body["reasoning_effort"] == "none" for body in sent)


@pytest.mark.parametrize(
    "defect,judge,triage",
    [("budget_spent", "fail", "pass"), ("reasoning_in_content", "fail", "fail")],
)
def test_reasoning_kept_on_fails_the_clients(tmp_path, defect, judge, triage):
    code, records = run_clients(tmp_path, reasoning_off_defect=defect)
    assert code == 1, records
    assert {k: r["outcome"] for k, r in records.items()} == {"judge": judge, "triage": triage}


def test_schema_case_misses_reasoning_in_content(tmp_path):
    """The schema case turns no reasoning off, so the defect never shows there."""
    with FakeFleet(reasoning_off_defect="reasoning_in_content") as fleet:
        path = write_profile(tmp_path, fleet, responses=False, structured_output=True)
        raw = yaml.safe_load(path.read_text())
        raw["ingresses"][0]["surfaces"] = ["chat_completions"]
        raw["capabilities"]["chat_reasoning_efforts"] = ["none"]
        path.write_text(yaml.safe_dump(raw))
        code, records = run_suites(path, tmp_path / "run", "-k", "test_structured_output")
    assert code == 0, records
    ran = [(r["case_id"], r["surface"], r["outcome"]) for r in records if r["outcome"] != "skip"]
    assert ran == [("field_options.structured_output", "chat_completions", "pass")], records


def test_clients_turn_reasoning_off_with_a_declared_quiet_effort(tmp_path):
    code, records = run_clients(tmp_path, efforts=("none",), quiet=("low",))
    assert code == 0, records
    for record in records.values():
        check = next(c for c in record["checks"] if c["name"] == "structured_client_shape")
        assert check["observed"]["reasoning_effort"] == "low", check


@pytest.mark.parametrize("efforts", [("low", "high"), ("high", "medium", "low")])
def test_a_model_with_no_off_state_sends_its_lowest_effort(tmp_path, efforts):
    """No quiet effort and no `none`: the clients still run, at the lowest declared effort,
    whatever the list order, and the case records that reasoning stays on."""
    code, records = run_clients(tmp_path, efforts=efforts)
    assert {r["outcome"] for r in records.values()} != {"skip"}, records
    for record in records.values():
        check = next(c for c in record["checks"] if c["name"] == "reasoning_stays_on")
        assert check["observed"] == {"reasoning_effort": "low"}, check
    sent = [
        json.loads(line)["request_body"]
        for line in (tmp_path / "run" / "exchanges.jsonl").read_text().splitlines()
    ]
    assert len(sent) == 2 and all(body["reasoning_effort"] == "low" for body in sent), sent


def test_a_reasoning_model_without_an_off_effort_skips(tmp_path):
    code, records = run_clients(tmp_path, efforts=())
    assert code == 0, records
    assert {r["outcome"] for r in records.values()} == {"skip"}
    assert all("[undeclared]" in r["reason"] for r in records.values()), records


TOOL_CASE = "scenarios.effort_tool_call"


def run_tool_calls(tmp_path, *, efforts=("low", "high"), auto=True, quiet=(), **fleet_kw):
    with FakeFleet(chat_tools=True, **fleet_kw) as fleet:
        path = write_profile(tmp_path, fleet, responses=False)
        raw = yaml.safe_load(path.read_text())
        raw["ingresses"][0]["surfaces"] = ["chat_completions"]
        raw["model"]["tools"] = {"parser": "example-parser", "auto_choice": auto}
        raw["capabilities"]["chat_reasoning_efforts"] = list(efforts)
        raw["capabilities"]["chat_quiet_efforts"] = list(quiet)
        path.write_text(yaml.safe_dump(raw))
        code, records = run_suites(path, tmp_path / "run", "-k", "test_effort_tool_call")
    by_variant = {r["variant"].rpartition("/")[2]: r for r in records if r["case_id"] == TOOL_CASE}
    return code, by_variant


def test_tool_calls_pass_on_a_healthy_fake(tmp_path):
    code, records = run_tool_calls(tmp_path)
    assert code == 0, records
    assert {k: r["outcome"] for k, r in records.items()} == {"default": "pass", "quiet": "pass"}
    sent = [
        json.loads(line)["request_body"]
        for line in (tmp_path / "run" / "exchanges.jsonl").read_text().splitlines()
    ]
    quiet, default = sorted(sent, key=lambda body: "stream" not in body)
    assert quiet["reasoning_effort"] == "low" and quiet["stream"] is True
    assert "reasoning_effort" not in default and default["max_tokens"] == 2048
    assert all(body["tool_choice"] == "auto" for body in sent)


@pytest.mark.parametrize(
    "defect,failed", [("markup_at_effort", "quiet"), ("runaway_default", "default")]
)
def test_a_tool_call_lost_at_one_effort_fails_that_variant(tmp_path, defect, failed):
    code, records = run_tool_calls(tmp_path, effort_tool_defect=defect)
    assert code == 1, records
    assert {k: r["outcome"] for k, r in records.items() if r["outcome"] == "fail"} == {
        failed: "fail"
    }


def test_forced_tool_loop_misses_both_effort_defects(tmp_path):
    """The forced loop sends a named choice and no effort, so neither defect shows there."""
    for defect in ("markup_at_effort", "runaway_default"):
        with FakeFleet(chat_tools=True, effort_tool_defect=defect) as fleet:
            path = write_profile(tmp_path / defect, fleet, responses=False)
            raw = yaml.safe_load(path.read_text())
            raw["ingresses"][0]["surfaces"] = ["chat_completions"]
            raw["model"]["tools"] = {"parser": "example-parser", "auto_choice": True}
            path.write_text(yaml.safe_dump(raw))
            code, records = run_suites(
                path, tmp_path / defect / "run", "-k", "test_forced_tool_loop"
            )
        assert code == 0, records
        assert {r["outcome"] for r in records if r["surface"] == "chat_completions"} == {"pass"}


def test_quiet_tool_call_prefers_a_declared_quiet_effort(tmp_path):
    code, records = run_tool_calls(tmp_path, efforts=("none", "high"), quiet=("low",))
    assert code == 0, records
    check = next(c for c in records["quiet"]["checks"] if c["name"] == "effort_tool_call_shape")
    assert check["observed"]["effort"] == "low", check


def test_tool_calls_skip_without_automatic_choice(tmp_path):
    code, records = run_tool_calls(tmp_path, auto=False)
    assert code == 0, records
    assert {r["outcome"] for r in records.values()} == {"skip"}
    assert all("[undeclared]" in r["reason"] for r in records.values()), records


NAMES_CASE = "scenarios.client_model_names"
CLIENT_NAME = "client-default-small"


def run_model_names(tmp_path, *, names=(CLIENT_NAME,), aliases=(), select=None, **fleet_kw):
    with FakeFleet(**fleet_kw) as fleet:
        path = write_profile(tmp_path, fleet, responses=True)
        raw = yaml.safe_load(path.read_text())
        raw["model"]["aliases"] = list(aliases)
        raw["capabilities"]["client_model_names"] = list(names)
        path.write_text(yaml.safe_dump(raw))
        code, records = run_suites(
            path, tmp_path / "run", "-k", select or "test_client_model_names"
        )
    return code, records


@pytest.mark.parametrize(
    "fleet_kw,aliases,outcome",
    [
        ({}, (), "pass"),
        ({"served_aliases": (CLIENT_NAME,)}, (CLIENT_NAME,), "pass"),
        # Forwarded to a backend that is not there.
        ({"model_errors": {CLIENT_NAME: 502}}, (), "fail"),
        # Quietly served by the fleet's own model.
        ({"fall_back_on_unknown_model": True}, (), "fail"),
        # Declared as an alias, refused by the fleet.
        ({}, (CLIENT_NAME,), "fail"),
    ],
)
def test_client_model_names_are_served_or_refused(tmp_path, fleet_kw, aliases, outcome):
    code, records = run_model_names(tmp_path, aliases=aliases, **fleet_kw)
    assert code == (1 if outcome == "fail" else 0), records
    by_surface = {r["surface"]: r["outcome"] for r in records if r["case_id"] == NAMES_CASE}
    assert set(by_surface) == {"chat_completions", "completions", "messages", "responses"}
    assert set(by_surface.values()) == {outcome}, records


def test_public_model_id_is_a_served_client_name(tmp_path):
    code, records = run_model_names(tmp_path, names=(MODEL,))
    assert code == 0, records
    assert {r["outcome"] for r in records if r["case_id"] == NAMES_CASE} == {"pass"}, records


def test_unknown_model_case_misses_a_name_forwarded_elsewhere(tmp_path):
    """The discovery case asks for a random name, so a layer that forwards one name passes it."""
    code, records = run_model_names(
        tmp_path,
        select="test_unknown_model_rejected_without_fallback",
        model_errors={CLIENT_NAME: 502},
    )
    assert code == 0, records
    assert {r["outcome"] for r in records} == {"pass"}, records


def test_client_model_names_skip_where_undeclared(tmp_path):
    code, records = run_model_names(tmp_path, names=())
    assert code == 0, records
    assert {r["outcome"] for r in records} == {"skip"}, records
    assert all("[undeclared]" in r["reason"] for r in records), records


@pytest.mark.parametrize("names", [[""], ["two words"], [CLIENT_NAME, CLIENT_NAME], CLIENT_NAME])
def test_client_model_names_are_strict(tmp_path, names):
    from llmdv.profile.loader import ProfileError, load

    with FakeFleet() as fleet:
        path = write_profile(tmp_path, fleet)
    raw = yaml.safe_load(path.read_text())
    raw["capabilities"]["client_model_names"] = names
    path.write_text(yaml.safe_dump(raw))
    with pytest.raises(ProfileError, match="client_model_names"):
        load(path)


QUIET_CASE = "scenarios.quiet_effort"


def run_quiet(tmp_path, *, quiet=("low",), options=("chat_template_kwargs",), **fleet_kw):
    with FakeFleet(quiet_efforts=("low",), **fleet_kw) as fleet:
        path = write_profile(tmp_path, fleet, responses=False)
        raw = yaml.safe_load(path.read_text())
        raw["ingresses"][0]["surfaces"] = ["chat_completions"]
        raw["capabilities"]["chat_quiet_efforts"] = list(quiet)
        raw["capabilities"]["vllm_options"] = list(options)
        path.write_text(yaml.safe_dump(raw))
        code, records = run_suites(path, tmp_path / "run", "-k", "test_quiet_effort")
    return code, {r["variant"].rpartition("/")[2]: r for r in records if r["case_id"] == QUIET_CASE}


@pytest.mark.parametrize(
    "defect,field,kwargs",
    [(None, "pass", "pass"), ("reasons", "fail", "pass"), ("kwargs_ignored", "pass", "fail")],
)
def test_quiet_effort_answers_without_reasoning(tmp_path, defect, field, kwargs):
    code, records = run_quiet(tmp_path, quiet_effort_defect=defect)
    assert code == (0 if defect is None else 1), records
    assert {k: r["outcome"] for k, r in records.items()} == {"field": field, "kwargs": kwargs}
    sent = [
        json.loads(line)["request_body"]
        for line in (tmp_path / "run" / "exchanges.jsonl").read_text().splitlines()
    ]
    assert {json.dumps(body.get("chat_template_kwargs")) for body in sent} == {
        "null",
        json.dumps({"reasoning_effort": "low"}),
    }


def test_quiet_effort_kwargs_skip_without_the_option(tmp_path):
    code, records = run_quiet(tmp_path, options=())
    assert code == 0, records
    assert records["field"]["outcome"] == "pass"
    assert records["kwargs"]["outcome"] == "skip"
    assert "[undeclared]" in records["kwargs"]["reason"]


def test_quiet_efforts_cannot_be_reasoning_efforts(tmp_path):
    from llmdv.profile.loader import ProfileError, load

    with FakeFleet() as fleet:
        path = write_profile(tmp_path, fleet)
    raw = yaml.safe_load(path.read_text())
    raw["capabilities"]["chat_quiet_efforts"] = ["low"]
    raw["capabilities"]["chat_reasoning_efforts"] = ["low", "high"]
    path.write_text(yaml.safe_dump(raw))
    with pytest.raises(ProfileError, match="chat_quiet_efforts"):
        load(path)
