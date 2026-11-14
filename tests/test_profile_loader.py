"""Profile loading and the offline diagnostics.

`validate` is the gate that keeps a bad profile from being mistaken for a bad fleet, so the
cases below are mostly about it reporting the right thing rather than merely failing.
"""

from __future__ import annotations

import textwrap

import pytest

from llmdv.profile import loader
from llmdv.profile.schema import (
    AuthType,
    Disposition,
    IngressKind,
    SkipClass,
    Vantage,
    Verifiable,
)

MINIMAL = """
schema: llmdv.profile/1
name: unit
model: {public_id: some/model}
ingresses:
  - id: direct
    kind: vllm
    base_url: http://localhost:9999
    surfaces: [chat_completions]
"""


def write(tmp_path, text):
    path = tmp_path / "p.yaml"
    path.write_text(textwrap.dedent(text))
    return path


def test_minimal_profile_loads_clean(tmp_path):
    profile = loader.load(write(tmp_path, MINIMAL))
    assert profile.name == "unit"
    assert profile.ingress_ids == ("direct",)
    assert profile.ingress("direct").kind is IngressKind.VLLM
    assert [d for d in loader.validate(profile) if d.level == "error"] == []


def test_negative_context_length_is_an_invalid_profile(tmp_path):
    profile = loader.load(
        write(
            tmp_path,
            MINIMAL.replace("public_id: some/model", "public_id: some/model, max_model_len: -1"),
        )
    )
    errors = [d for d in loader.validate(profile) if d.level == "error"]
    assert errors == [
        loader.Diagnostic("error", "model.max_model_len", "must not be negative"),
        loader.Diagnostic("error", "ingresses(direct).model.max_model_len", "must not be negative"),
    ]


@pytest.mark.parametrize("value", ["0", "-1", "true", "false", "1.5", '"37"', '""', "[]", "{}"])
def test_answer_budget_rejects_nonpositive_or_noninteger_declarations(tmp_path, value):
    path = write(
        tmp_path,
        MINIMAL.replace(
            "public_id: some/model", f"public_id: some/model, answer_max_tokens: {value}"
        ),
    )
    with pytest.raises(
        loader.ProfileError, match=r"model\.answer_max_tokens: expected a positive integer, got"
    ):
        loader.load(path)


@pytest.mark.parametrize(
    ("declaration", "expected"),
    [("", None), (", answer_max_tokens: null", None), (", answer_max_tokens: 37", 37)],
)
def test_answer_budget_is_optional_and_loaded_exactly(tmp_path, declaration, expected):
    profile = loader.load(
        write(
            tmp_path,
            MINIMAL.replace("public_id: some/model", f"public_id: some/model{declaration}"),
        )
    )
    assert profile.model.answer_max_tokens == expected
    assert not [d for d in loader.validate(profile) if d.level == "error"]


@pytest.mark.parametrize(
    ("declaration", "expected"),
    [
        ("", "tool_calls"),
        (", tools: {forced_finish_reason: null}", "tool_calls"),
        (", tools: {forced_finish_reason: stop}", "stop"),
        (", tools: {forced_finish_reason: tool_calls}", "tool_calls"),
    ],
)
def test_forced_tool_finish_defaults_to_tool_calls(tmp_path, declaration, expected):
    profile = loader.load(
        write(
            tmp_path,
            MINIMAL.replace("public_id: some/model", f"public_id: some/model{declaration}"),
        )
    )
    assert profile.model.tools.forced_finish_reason == expected
    assert profile.ingress("direct").model.tools.forced_finish_reason == expected


@pytest.mark.parametrize("value", ["end_turn", "tool_use", "Stop", "true", "[stop]"])
def test_forced_tool_finish_rejects_other_values(tmp_path, value):
    path = write(
        tmp_path,
        MINIMAL.replace(
            "public_id: some/model",
            f"public_id: some/model, tools: {{forced_finish_reason: {value}}}",
        ),
    )
    with pytest.raises(loader.ProfileError, match=r"model\.tools\.forced_finish_reason: expected"):
        loader.load(path)


@pytest.mark.parametrize(
    ("declaration", "expected", "every"),
    [
        ("", "auto", False),
        (", tools: {strict_level: null}", "auto", False),
        (", tools: {strict_level: function}", "function", False),
        (", tools: {strict_level: parameter}", "parameter", True),
    ],
)
def test_tool_strict_level_defaults_to_auto(tmp_path, declaration, expected, every):
    profile = loader.load(
        write(
            tmp_path,
            MINIMAL.replace("public_id: some/model", f"public_id: some/model{declaration}"),
        )
    )
    assert profile.model.tools.strict_level == expected
    assert profile.model.tools.enforces_every_schema is every


@pytest.mark.parametrize("value", ["strict", "Function", "true", "[parameter]"])
def test_tool_strict_level_rejects_other_values(tmp_path, value):
    path = write(
        tmp_path,
        MINIMAL.replace(
            "public_id: some/model", f"public_id: some/model, tools: {{strict_level: {value}}}"
        ),
    )
    with pytest.raises(loader.ProfileError, match=r"model\.tools\.strict_level: expected"):
        loader.load(path)


def test_missing_file_raises_profile_error():
    with pytest.raises(loader.ProfileError, match="no such profile file"):
        loader.load("/nonexistent/profile.yaml")


def test_invalid_yaml_raises_profile_error(tmp_path):
    with pytest.raises(loader.ProfileError, match="invalid YAML"):
        loader.load(write(tmp_path, "name: [unclosed\n"))


def test_bad_enum_names_the_allowed_values(tmp_path):
    with pytest.raises(loader.ProfileError, match="is not one of"):
        loader.load(write(tmp_path, MINIMAL.replace("kind: vllm", "kind: not_a_kind")))


def test_non_http_base_url_is_an_error(tmp_path):
    profile = loader.load(write(tmp_path, MINIMAL.replace("http://localhost:9999", "ftp://x")))
    errors = [d for d in loader.validate(profile) if d.level == "error"]
    assert any("base_url" in d.path for d in errors)


def test_client_needing_an_unexposed_surface_is_dead_config(tmp_path):
    """This is the check that catches config no runtime would ever complain about."""
    profile = loader.load(
        write(
            tmp_path,
            MINIMAL
            + """
clients:
  - {id: ghost, surfaces: [messages]}
""",
        )
    )
    errors = [d for d in loader.validate(profile) if d.level == "error"]
    assert any("no ingress in this profile exposes" in d.message for d in errors)


def test_header_naming_an_undeclared_ingress_is_an_error(tmp_path):
    profile = loader.load(
        write(
            tmp_path,
            MINIMAL
            + """
headers:
  x-request-id: {nosuch: echoed}
""",
        )
    )
    errors = [d for d in loader.validate(profile) if d.level == "error"]
    assert any("not declared" in d.message for d in errors)


def test_declared_but_unimplemented_surface_is_a_warning_not_an_error(tmp_path, monkeypatch):
    """A profile may honestly describe a fleet before llmdv can drive all of it."""
    profile = loader.load(
        write(
            tmp_path,
            MINIMAL.replace(
                "surfaces: [chat_completions]", "surfaces: [chat_completions, chat_render]"
            ),
        )
    )
    monkeypatch.setattr(loader.surfaces, "implemented", lambda: frozenset({"chat_completions"}))
    diagnostics = loader.validate(profile)
    assert [d for d in diagnostics if d.level == "error"] == []
    assert any("not implemented" in d.message for d in diagnostics)


def test_auth_without_key_env_is_an_error(tmp_path):
    profile = loader.load(
        write(
            tmp_path,
            MINIMAL
            + """
    auth: {type: bearer}
""",
        )
    )
    errors = [d for d in loader.validate(profile) if d.level == "error"]
    assert any("requires key_env" in d.message for d in errors)


def test_secret_is_read_from_the_declared_env_var(tmp_path, monkeypatch):
    profile = loader.load(
        write(
            tmp_path,
            MINIMAL
            + """
    auth: {type: bearer, key_env: LLMDV_UNIT_KEY}
""",
        )
    )
    monkeypatch.delenv("LLMDV_UNIT_KEY", raising=False)
    assert loader.resolve_secret(profile.ingress("direct")) is None
    monkeypatch.setenv("LLMDV_UNIT_KEY", "sk-unit")
    assert loader.resolve_secret(profile.ingress("direct")) == "sk-unit"
    assert profile.ingress("direct").auth.header("sk-unit") == {"Authorization": "Bearer sk-unit"}


def test_nullable_reasoning_parser_with_inline_tag(tmp_path):
    """The no-parser shape: reasoning arrives inline in content."""
    profile = loader.load(
        write(
            tmp_path,
            MINIMAL
            + """
    timeouts: {connect: 1, request: 2, stream_idle: 3}
""",
        )
    )
    assert profile.model.reasoning.parser is None
    assert profile.ingress("direct").timeouts.request == 2

    inline = loader.load(
        write(
            tmp_path,
            """
schema: llmdv.profile/1
name: inline
model:
  public_id: m
  reasoning: {parser: null, inline_tag: think, default_on: true}
ingresses:
  - {id: a, kind: vllm, base_url: 'http://x:1', surfaces: [chat_completions]}
""",
        )
    )
    assert inline.model.reasoning.is_inline
    assert not inline.model.reasoning.is_separate_field


def test_error_status_accepts_a_scalar_or_a_list(tmp_path):
    profile = loader.load(
        write(
            tmp_path,
            MINIMAL
            + """
    error_status: {unknown_model: 404, unknown_path: [404, 403]}
""",
        )
    )
    ingress = profile.ingress("direct")
    assert ingress.expected_error_status("unknown_model") == (404,)
    assert ingress.expected_error_status("unknown_path") == (404, 403)
    assert ingress.expected_error_status("wrong_method") is None


def test_unknown_error_class_is_an_error(tmp_path):
    profile = loader.load(
        write(
            tmp_path,
            MINIMAL
            + """
    error_status: {not_a_class: 400}
""",
        )
    )
    errors = [d for d in loader.validate(profile) if d.level == "error"]
    assert any("unknown error class" in d.message for d in errors)


def test_vision_with_a_text_only_image_status_is_an_error(tmp_path):
    text_only = MINIMAL + "    error_status: {image_input: 400}\n"
    both = loader.load(write(tmp_path, text_only + "capabilities:\n  vision: true\n"))
    errors = [d for d in loader.validate(both) if d.level == "error"]
    assert any("text-only" in d.message for d in errors)
    alone = loader.load(write(tmp_path, text_only))
    assert [d for d in loader.validate(alone) if d.level == "error"] == []


def test_auth_type_none_sends_no_header():
    from llmdv.profile.schema import Auth

    assert Auth(type=AuthType.NONE).header("ignored") == {}
    assert Auth(type=AuthType.API_KEY, key_env="K").header("sk") == {"x-api-key": "sk"}


def test_disposition_observability_maps_to_skip_classes():
    """The spec's disposition table, as executable truth."""
    assert Disposition.ECHOED.client_observable
    assert Disposition.IGNORED.client_observable
    assert not Disposition.REWRITTEN.client_observable
    assert Disposition.REWRITTEN.verifiable_by is Verifiable.INSTRUMENTED_BACKEND
    assert SkipClass.for_disposition(Disposition.REWRITTEN) is SkipClass.REQUIRES_INSTRUMENTATION
    assert SkipClass.for_disposition(Disposition.CONSUMED) is SkipClass.REQUIRES_LOAD
    assert SkipClass.for_disposition(Disposition.ECHOED) is None


def test_a_profile_still_setting_the_removed_responses_capability_loads(tmp_path):
    """The removed key is ignored like any other unknown key, so an existing profile still runs."""
    profile = loader.load(write(tmp_path, MINIMAL + "capabilities:\n  responses: true\n"))
    assert [d for d in loader.validate(profile) if d.level == "error"] == []
    assert not hasattr(profile.capabilities, "responses")


@pytest.mark.parametrize(
    ("declaration", "expected"),
    [
        ("true", True),
        ("false", False),
        ("null", False),
        ("[]", ()),
        ("[chat_completions]", ("chat_completions",)),
        ("[chat_completions, responses]", ("chat_completions", "responses")),
    ],
)
def test_structured_output_accepts_booleans_or_surface_lists(tmp_path, declaration, expected):
    profile = loader.load(
        write(tmp_path, MINIMAL + f"capabilities:\n  structured_output: {declaration}\n")
    )
    assert profile.capabilities.structured_output == expected


@pytest.mark.parametrize(
    "declaration",
    [
        '"false"',
        '"true"',
        "0",
        "1",
        "chat_completions",
        "{}",
        "[chat_completion]",
        "[chat_completions, unknown]",
        "[true]",
        "[1]",
        "[null]",
        "[{}]",
        "[[]]",
    ],
)
def test_invalid_structured_output_declarations_fail_during_load(tmp_path, declaration):
    with pytest.raises(loader.ProfileError, match=r"capabilities\.structured_output"):
        loader.load(
            write(tmp_path, MINIMAL + f"capabilities:\n  structured_output: {declaration}\n")
        )


# ---------------------------------------------------------------- vantage


def test_vantage_names_a_position_not_a_provenance(tmp_path):
    """`external` is any client outside the cluster: a laptop, a CI runner, a jump host.

    It was called `laptop`, which stopped being true the moment CI drove a run. What drove it
    is a separate axis and belongs in `--label`, so no `ci` member exists here -- a CI job can
    occupy either position, including in-cluster.
    """
    assert [member.value for member in Vantage] == ["external", "in-cluster"]
    with pytest.raises(ValueError):
        Vantage("ci")


def test_laptop_is_accepted_as_a_deprecated_alias(tmp_path):
    """Profiles predating the rename must keep loading rather than failing validation."""
    assert Vantage("laptop") is Vantage.EXTERNAL
    path = tmp_path / "old.yaml"
    path.write_text(
        textwrap.dedent("""
            schema: llmdv.profile/1
            name: old
            vantage: laptop
            model: {public_id: m}
            ingresses:
              - id: a
                kind: vllm
                base_url: http://localhost:1
                surfaces: [chat_completions]
        """)
    )
    assert loader.load(path).vantage is Vantage.EXTERNAL


def test_vantage_defaults_to_external(tmp_path):
    path = tmp_path / "bare.yaml"
    path.write_text(
        textwrap.dedent("""
            schema: llmdv.profile/1
            name: bare
            model: {public_id: m}
            ingresses:
              - id: a
                kind: vllm
                base_url: http://localhost:1
                surfaces: [chat_completions]
        """)
    )
    assert loader.load(path).vantage is Vantage.EXTERNAL
