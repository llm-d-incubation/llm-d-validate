"""One profile can describe different model names and contracts at each ingress."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest
import yaml

from llmdv.cli import banner
from llmdv.profile import loader
from tests.fakes import FakeFleet, profile_yaml, run_llmdv
from tests.test_suites_against_fake import SUITES, run_pytest, run_suites


def configuration():
    return {
        "model": {
            "public_id": "engine-model",
            "aliases": ["engine-alias"],
            "max_model_len": 4096,
            "answer_max_tokens": 37,
            "reasoning": {"parser": "parser-a", "field": "reasoning_content", "default_on": True},
            "tools": {"parser": "tool-parser", "auto_choice": True},
        },
        "capabilities": {
            "count_tokens": True,
            "stateful_responses": True,
            "structured_output": ["chat_completions", "responses"],
            "vllm_options": ["priority", "cache_salt"],
        },
        "ingresses": [
            {"id": "engine", "base_url": "http://localhost:1", "surfaces": ["chat_completions"]},
            {"id": "gateway", "base_url": "http://localhost:2", "surfaces": ["chat_completions"]},
        ],
    }


def write(tmp_path, config):
    path = tmp_path / "profile.yaml"
    path.write_text(yaml.safe_dump(config))
    return path


def two_fleets(tmp_path, first, second, **overrides):
    config = yaml.safe_load(profile_yaml(first.base_url, first.model))
    config["ingresses"].append(
        {
            **deepcopy(config["ingresses"][0]),
            "id": "gateway",
            "base_url": second.base_url,
            **overrides,
        }
    )
    config["headers"]["x-request-id"]["gateway"] = "ignored"
    return write(tmp_path, config)


def read_jsonl(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_nested_fields_inherit_and_explicit_values_replace_without_mutating_defaults(tmp_path):
    config = configuration()
    config["ingresses"][1].update(
        model={
            "public_id": "gateway-model",
            "aliases": [],
            "max_model_len": None,
            "answer_max_tokens": None,
            "reasoning": {
                "parser": None,
                "field": None,
                "inline_tag": "think",
                "default_on": False,
            },
            "tools": {"parser": None},
        },
        capabilities={"count_tokens": False, "structured_output": ["messages"], "vllm_options": []},
    )
    profile = loader.load(write(tmp_path, config))
    engine, gateway = (profile.ingress(name) for name in ("engine", "gateway"))
    assert engine.model == profile.model
    assert engine.capabilities == profile.capabilities
    assert engine.model.public_id == "engine-model"
    assert engine.model.aliases == ("engine-alias",)
    assert engine.model.max_model_len == 4096
    assert engine.model.reasoning.parser == "parser-a"
    assert engine.model.reasoning.default_on
    assert engine.capabilities.count_tokens
    assert engine.capabilities.structured_output == ("chat_completions", "responses")
    assert gateway.model.public_id == "gateway-model"
    assert gateway.model.aliases == ()
    assert gateway.model.max_model_len is gateway.model.answer_max_tokens is None
    assert gateway.model.reasoning.parser is gateway.model.reasoning.field_name is None
    assert gateway.model.reasoning.inline_tag == "think"
    assert not gateway.model.reasoning.default_on
    assert not gateway.model.tools.supported
    assert (
        gateway.model.tools.auto_choice
    )  # Omitted nested siblings inherit, even with a null parser.
    assert not gateway.capabilities.count_tokens
    assert gateway.capabilities.stateful_responses
    assert gateway.capabilities.structured_output == ("messages",)
    assert gateway.capabilities.vllm_options == ()
    assert loader.validate(profile) == []
    with pytest.raises(FrozenInstanceError):
        gateway.model.public_id = "changed"
    assert profile.ingress("engine") == engine


def test_only_a_public_id_override_inherits_every_other_field(tmp_path):
    config = configuration()
    config["ingresses"][1]["model"] = {"public_id": "gateway-model"}
    profile = loader.load(write(tmp_path, config))
    model = profile.ingress("gateway").model
    assert model.public_id == "gateway-model"
    assert model.aliases == profile.model.aliases
    assert model.max_model_len == profile.model.max_model_len
    assert model.answer_max_tokens == profile.model.answer_max_tokens
    assert model.reasoning == profile.model.reasoning
    assert model.tools == profile.model.tools
    assert profile.ingress("gateway").capabilities == profile.capabilities


def test_forced_tool_finish_is_declared_per_ingress(tmp_path):
    config = configuration()
    config["ingresses"][1]["model"] = {"tools": {"forced_finish_reason": "stop"}}
    profile = loader.load(write(tmp_path, config))
    engine, gateway = (profile.ingress(name).model.tools for name in ("engine", "gateway"))
    assert engine.forced_finish_reason == "tool_calls"
    assert gateway.forced_finish_reason == "stop"
    assert gateway.parser == engine.parser == "tool-parser"


@pytest.mark.parametrize("value", [True, False, [], ["messages"]])
def test_structured_output_override_preserves_boolean_or_surface_list(tmp_path, value):
    config = configuration()
    config["ingresses"][1]["capabilities"] = {"structured_output": value}
    profile = loader.load(write(tmp_path, config))
    actual = profile.ingress("gateway").capabilities.structured_output
    assert actual == (tuple(value) if isinstance(value, list) else value)


@pytest.mark.parametrize(
    "name",
    ["model_retrieval", "responses_input_tokens", "responses_compact"],
)
def test_nullable_capability_override_clears_a_tri_state_default(tmp_path, name):
    config = configuration()
    config["capabilities"][name] = True
    config["ingresses"][1]["capabilities"] = {name: None}
    profile = loader.load(write(tmp_path, config))
    assert getattr(profile.ingress("engine").capabilities, name) is True
    assert getattr(profile.ingress("gateway").capabilities, name) is None


def test_null_model_info_override_clears_an_inherited_mapping(tmp_path):
    config = configuration()
    config["capabilities"]["model_info"] = {"supports_vision": False}
    config["ingresses"][1]["capabilities"] = {"model_info": None}
    profile = loader.load(write(tmp_path, config))
    assert profile.ingress("engine").capabilities.model_info == {"supports_vision": False}
    assert profile.ingress("gateway").capabilities.model_info == {}


@pytest.mark.parametrize(
    ("block", "value", "field"),
    [
        ("model", None, "model"),
        ("model", [], "model"),
        ("model", {"typo": True}, "model.typo"),
        ("model", {"public_id": 123}, "model.public_id"),
        ("model", {"public_id": None}, "model.public_id"),
        ("model", {"aliases": "alias"}, "model.aliases"),
        ("model", {"aliases": [123]}, "model.aliases[0]"),
        ("model", {"max_model_len": "4096"}, "model.max_model_len"),
        ("model", {"max_model_len": True}, "model.max_model_len"),
        ("model", {"answer_max_tokens": "256"}, "model.answer_max_tokens"),
        ("model", {"reasoning": None}, "model.reasoning"),
        ("model", {"reasoning": []}, "model.reasoning"),
        ("model", {"reasoning": {"default_on": "false"}}, "model.reasoning.default_on"),
        ("model", {"reasoning": {"field_name": "x"}}, "model.reasoning.field_name"),
        ("model", {"reasoning": {"parser": []}}, "model.reasoning.parser"),
        ("model", {"tools": {"auto_choice": None}}, "model.tools.auto_choice"),
        ("model", {"tools": {"forced_finish_reason": None}}, "model.tools.forced_finish_reason"),
        (
            "model",
            {"tools": {"forced_finish_reason": "end_turn"}},
            "model.tools.forced_finish_reason",
        ),
        ("capabilities", None, "capabilities"),
        ("capabilities", [], "capabilities"),
        ("capabilities", {"count_token": True}, "capabilities.count_token"),
        ("capabilities", {"count_tokens": "false"}, "capabilities.count_tokens"),
        ("capabilities", {"count_tokens": None}, "capabilities.count_tokens"),
        ("capabilities", {"structured_output": ["bogus"]}, "capabilities.structured_output[0]"),
        ("capabilities", {"structured_output": "messages"}, "capabilities.structured_output"),
        ("capabilities", {"vllm_options": "priority"}, "capabilities.vllm_options"),
    ],
)
def test_invalid_override_fails_during_loading_with_its_ingress_path(tmp_path, block, value, field):
    config = configuration()
    config["ingresses"][1][block] = value
    with pytest.raises(loader.ProfileError) as exc:
        loader.load(write(tmp_path, config))
    assert f"ingresses(gateway).{field}:" in str(exc.value)


@pytest.mark.parametrize("entry_point", ["pytest", "verify"])
@pytest.mark.parametrize(
    "bad_model, field",
    [
        ({"answer_max_tokens": "256"}, "answer_max_tokens"),
        ({"max_model_len": -1}, "max_model_len"),
        ({"public_id": ""}, "public_id"),
    ],
)
def test_bad_override_is_a_usage_error_before_network_access(
    tmp_path, entry_point, bad_model, field
):
    with FakeFleet() as first, FakeFleet(model="gateway-model") as second:
        path = two_fleets(tmp_path, first, second, model=bad_model)
        if entry_point == "pytest":
            result, records = run_pytest(SUITES, path, tmp_path / "run", "-k", "basic")
            assert records == []
            expected = pytest.ExitCode.USAGE_ERROR
        else:
            result = run_llmdv("verify", "--profile", str(path), "--pod-logs", "off")
            expected = 2
        assert not first.seen and not second.seen
    assert result.returncode == expected, result.stdout + result.stderr
    assert f"ingresses(gateway).model.{field}" in result.stderr


def test_all_suites_use_each_ingress_model_including_sdk_and_header_probes(tmp_path):
    with FakeFleet() as first, FakeFleet(model="gateway-model", max_model_len=2048) as second:
        path = two_fleets(
            tmp_path,
            first,
            second,
            model={
                "public_id": second.model,
                "max_model_len": 2048,
                "answer_max_tokens": 37,
                "reasoning": {"default_on": False},
            },
            capabilities={"structured_output": True},
        )
        run_dir = tmp_path / "run"
        result = run_llmdv(
            "verify",
            "--profile",
            str(path),
            "--local-output",
            str(run_dir),
            "--pod-logs",
            "off",
        )
    records = read_jsonl(run_dir / "results.jsonl")
    assert result.returncode == 0, result.stdout + result.stderr
    for record in records:
        assert record["outcome"] in {"pass", "skip"}, record
    by_pair = {(r["ingress"], r["case_id"]): r for r in records}
    for ingress in ("fake", "gateway"):
        for case in (
            "contract.header_disposition",
            "contract.declared_surfaces_exist",
            "discovery.models_lists_declared_model",
            "discovery.models_parses_with_openai_sdk",
            "openai_chat.basic_non_streaming",
            "openai_chat.basic_parses_with_openai_sdk",
            "anthropic_messages.basic_non_streaming",
            "anthropic_messages.basic_parses_with_anthropic_sdk",
            "openai_responses.streaming_sse",
            "anthropic_messages.streaming_sse",
        ):
            assert by_pair[ingress, case]["outcome"] == "pass"
    traffic = read_jsonl(run_dir / "exchanges.jsonl")
    for exchange in traffic:
        body = exchange["request_body"]
        if not isinstance(body, dict) or str(body.get("model", "")).startswith(
            "llmdv-nonexistent-"
        ):
            continue
        if "model" in body:
            assert body["model"] == (
                second.model if exchange["ingress"] == "gateway" else first.model
            )
    for ingress, budget in (("fake", 256), ("gateway", 64)):
        smoke = next(
            e
            for e in traffic
            if e["ingress"] == ingress and e["case_id"] == "openai_chat.basic_non_streaming"
        )
        assert smoke["request_body"]["max_tokens"] == budget
    budget = next(
        e
        for e in traffic
        if e["ingress"] == "gateway" and e["case_id"] == "openai_chat.answer_budget"
    )
    assert budget["request_body"]["max_tokens"] == 37
    env = json.loads((run_dir / "env.json").read_text())
    assert env["ingress_models"]["fake"]["model"]["public_id"] == first.model
    assert env["ingress_models"]["gateway"]["model"]["public_id"] == second.model
    assert env["ingress_models"]["gateway"]["model"]["max_model_len"] == 2048
    assert "model=gateway-model" in result.stdout


def test_capability_overrides_gate_only_the_selected_ingress_and_surface(tmp_path):
    with FakeFleet() as first, FakeFleet(model="gateway-model") as second:
        path = two_fleets(
            tmp_path,
            first,
            second,
            model={"public_id": second.model},
            capabilities={"count_tokens": False, "structured_output": ["messages"]},
        )
        run_dir = tmp_path / "run"
        code, records = run_suites(path, run_dir, "-k", "count_tokens or structured_output")
    assert code == 0, records
    for record in records:
        count = "count_tokens" in record["case_id"]
        expected = (count and record["ingress"] == "fake") or (
            not count and record["ingress"] == "gateway" and record["surface"] == "messages"
        )
        assert record["outcome"] == ("pass" if expected else "skip"), record
        if not expected:
            assert record["skip_class"] == "undeclared"
    gateway = [e for e in read_jsonl(run_dir / "exchanges.jsonl") if e["ingress"] == "gateway"]
    assert len(gateway) == 1
    assert gateway[0]["url"].endswith("/v1/messages")


@pytest.mark.parametrize("sdk", [False, True])
def test_overridden_model_is_still_checked_against_the_response(tmp_path, sdk):
    with (
        FakeFleet() as first,
        FakeFleet(model="gateway-model", chat_overrides={"model": first.model}) as second,
    ):
        path = two_fleets(tmp_path, first, second, model={"public_id": second.model})
        code, records = run_suites(
            path, tmp_path / "run", "-k", "test_chat_basic and " + ("sdk" if sdk else "not sdk")
        )
    assert code == 1
    engine, gateway = records
    assert engine["outcome"] == "pass"
    assert gateway["outcome"] == "fail"
    check = next(c for c in gateway["checks"] if c["name"] == "model_echo")
    assert not check["ok"] and check["observed"] == first.model
    assert second.model in gateway["reason"]


@pytest.mark.parametrize("matrix", [False, True])
def test_profile_show_makes_each_effective_contract_visible(tmp_path, matrix):
    config = configuration()
    config["ingresses"][1].update(
        model={"public_id": "gateway-model", "reasoning": {"parser": None, "field": None}},
        capabilities={"count_tokens": False},
    )
    path = write(tmp_path, config)
    result = run_llmdv("profile", "show", str(path), *(["--matrix"] if matrix else []))
    assert result.returncode == 0, result.stderr
    assert "model=engine-model" in result.stdout
    assert "model=gateway-model" in result.stdout
    assert '"count_tokens": true' in result.stdout
    assert '"count_tokens": false' in result.stdout
    assert '"parser": null' in result.stdout
    assert '"field": null' in result.stdout
    assert "field_name" not in result.stdout
    if not matrix:
        assert "defaults  engine-model" in result.stdout


def test_clearing_token_limits_skips_only_that_ingress_without_requests(tmp_path):
    with FakeFleet() as first, FakeFleet(model="gateway-model") as second:
        path = two_fleets(
            tmp_path,
            first,
            second,
            model={"public_id": second.model, "max_model_len": None, "answer_max_tokens": None},
        )
        config = yaml.safe_load(path.read_text())
        config["model"]["answer_max_tokens"] = 37
        write(tmp_path, config)
        code, records = run_suites(
            path, tmp_path / "run", "-k", "context_overflow or answer_budget"
        )
        assert first.seen and not second.seen
    assert code == 0, records
    assert len(records) == 12
    for record in records:
        assert record["outcome"] == ("pass" if record["ingress"] == "fake" else "skip")
        if record["ingress"] == "gateway":
            assert record["skip_class"] == "undeclared"
            assert "ingress 'gateway' declares no" in record["reason"]


def test_nested_model_and_capability_overrides_drive_needs_gates(tmp_path):
    suite = tmp_path / "suite"
    suite.mkdir()
    (suite / "test_needs.py").write_text("""
import pytest
pytestmark = pytest.mark.surface("chat_completions")

@pytest.mark.needs("reasoning")
def test_reasoning(model, ingress):
    assert model is ingress.model

@pytest.mark.needs("tools")
def test_tools(model):
    assert model.tools.supported

@pytest.mark.needs("stateful_responses")
def test_stateful(ingress):
    assert ingress.capabilities.stateful_responses

@pytest.mark.needs("stateful_responses")
def test_defaults_without_an_ingress(profile):
    assert profile.capabilities.stateful_responses
""")
    config = configuration()
    config["ingresses"][1].update(
        model={"reasoning": {"parser": None, "field": None}, "tools": {"parser": None}},
        capabilities={"stateful_responses": False},
    )
    result, records = run_pytest(suite, write(tmp_path, config), tmp_path / "run")
    assert result.returncode == 0, result.stdout + result.stderr
    assert len(records) == 7
    assert sum(r["ingress"] is None for r in records) == 1
    for record in records:
        assert record["outcome"] == ("skip" if record["ingress"] == "gateway" else "pass")
        if record["ingress"] == "gateway":
            assert record["skip_class"] == "undeclared"
            assert "gateway" in record["reason"]


def test_selected_ingress_uses_its_contract_in_requests_and_metadata(tmp_path):
    with FakeFleet() as first, FakeFleet(model="gateway-model") as second:
        path = two_fleets(
            tmp_path,
            first,
            second,
            model={"public_id": second.model},
            capabilities={"structured_output": ["messages"]},
        )
        # The engine's additional advertised name must not leak into gateway discovery.
        config = yaml.safe_load(path.read_text())
        config["model"]["aliases"] = [first.model]
        config["ingresses"][1]["model"]["aliases"] = []
        write(tmp_path, config)
        run_dir = tmp_path / "run"
        result = run_llmdv(
            "verify",
            "--profile",
            str(path),
            "--ingress",
            "gateway",
            "--local-output",
            str(run_dir),
            "--pod-logs",
            "off",
            "-k",
            "test_models_lists_declared_model or test_structured_output",
        )
        assert not first.seen
    assert result.returncode == 0, result.stdout + result.stderr
    records = read_jsonl(run_dir / "results.jsonl")
    assert {r["ingress"] for r in records} == {"gateway"}
    assert (
        next(r for r in records if r["case_id"] == "discovery.models_lists_declared_model")[
            "outcome"
        ]
        == "pass"
    )
    ingress_models = json.loads((run_dir / "env.json").read_text())["ingress_models"]
    assert set(ingress_models) == {"gateway"}
    assert ingress_models["gateway"]["model"]["aliases"] == []
    assert ingress_models["gateway"]["capabilities"]["structured_output"] == ["messages"]


def test_chat_stream_rejects_an_echo_of_the_default_instead_of_the_override(tmp_path):
    with FakeFleet() as first, FakeFleet(fall_back_on_unknown_model=True) as second:
        path = two_fleets(tmp_path, first, second, model={"public_id": "gateway-model"})
        code, records = run_suites(path, tmp_path / "run", "-k", "test_streaming_sse")
    assert code == 1, records
    engine, gateway = records
    assert engine["outcome"] == "pass"
    assert gateway["outcome"] == "fail"
    assert "gateway-model" in gateway["reason"]
    assert any(c["name"] == "model_echo" and not c["ok"] for c in gateway["checks"])


@pytest.mark.parametrize("location", ["defaults", "gateway"])
@pytest.mark.parametrize(
    "settings, suffix, level, message",
    [
        ({"public_id": ""}, "public_id", "error", "required"),
        ({"max_model_len": -1}, "max_model_len", "error", "must not be negative"),
        (
            {"reasoning": {"field": "reasoning_content", "inline_tag": "think"}},
            "reasoning",
            "warning",
            "both field and inline_tag are set; the field is preferred when present",
        ),
    ],
)
def test_model_validation_uses_the_same_rules_and_paths_for_defaults_and_ingresses(
    tmp_path, location, settings, suffix, level, message
):
    config = configuration()
    if location == "defaults":
        config["model"].update(settings)
        paths = ("model", "ingresses(engine).model", "ingresses(gateway).model")
    else:
        config["ingresses"][1]["model"] = settings
        paths = ("ingresses(gateway).model",)
    profile = loader.load(write(tmp_path, config))
    assert loader.validate(profile) == [
        loader.Diagnostic(level, f"{path}.{suffix}", message) for path in paths
    ]


@pytest.mark.parametrize("field, value", [("max_model_len", "8192"), ("public_id", 123)])
def test_top_level_model_values_are_not_coerced(tmp_path, field, value):
    config = configuration()
    config["model"][field] = value
    with pytest.raises(loader.ProfileError) as exc:
        loader.load(write(tmp_path, config))
    assert f"model.{field}: expected" in str(exc.value)


def test_a_later_ingress_still_inherits_unmodified_defaults(tmp_path):
    config = configuration()
    config["ingresses"][1]["model"] = {"reasoning": {"parser": None, "field": None}, "aliases": []}
    config["ingresses"][1]["capabilities"] = {"structured_output": []}
    config["ingresses"].append({**config["ingresses"][0], "id": "later"})
    profile = loader.load(write(tmp_path, config))
    assert profile.ingress("later").model == profile.model
    assert profile.ingress("later").capabilities == profile.capabilities
    assert profile.ingress("gateway").model.reasoning.parser is None
    assert profile.ingress("gateway").capabilities.structured_output == ()


@pytest.mark.parametrize("explicit_values", [False, True])
def test_equal_overrides_do_not_label_profile_output_as_different(
    tmp_path, explicit_values, monkeypatch
):
    config = configuration()
    config["ingresses"][1]["model"] = deepcopy(config["model"]) if explicit_values else {}
    config["ingresses"][1]["capabilities"] = (
        deepcopy(config["capabilities"]) if explicit_values else {}
    )
    path = write(tmp_path, config)
    result = run_llmdv("profile", "show", str(path))
    assert result.returncode == 0, result.stderr
    assert "model     engine-model" in result.stdout
    assert "defaults  engine-model" not in result.stdout

    output = []
    monkeypatch.setattr(banner, "emit", output.append)
    banner.start(
        loader.load(path),
        str(path),
        ["gateway"],
        SimpleNamespace(ingress=None, client=None, surface=None, k=None, m=None),
        tmp_path / "run",
        [],
    )
    assert not any(line.lstrip().startswith("defaults") for line in output)
    assert not any("capabilities=" in line for line in output)
