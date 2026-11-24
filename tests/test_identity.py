import pytest

from llmdv.identity import IdentityError, build, differences, parse, validate_lane


def test_lane_identity_defaults_to_manual_and_accepts_the_ref_alias():
    assert parse(["accelerator=amd", "llm-d-ref=guide-1"]) == {
        "accelerator": "amd",
        "llm_d_ref": "guide-1",
        "trigger": "manual",
    }


def test_unknown_identity_dimensions_are_usage_errors():
    with pytest.raises(IdentityError, match="llmdv_version"):
        parse(["llmdv_version=old"])


def test_profile_dimensions_are_accepted_as_assertions():
    assert parse(["model=model-a", "engine=vllm"]) == {
        "model": "model-a",
        "engine": "vllm",
        "trigger": "manual",
    }


def test_profile_assertion_mismatch_is_a_usage_error():
    with pytest.raises(IdentityError, match="model"):
        validate_lane({"model": "wrong"}, {"model": "model-a"})


def test_identity_takes_engine_and_gateway_from_the_top_level_fields():
    """The product behind a layer is named at the top level, where a profile whose engine is
    not one of its ingresses (a guide profile tested at the router) can still state it."""

    class Profile:
        guide = "guide-a"
        engine = "sglang"
        gateway = "kong"
        model = type("Model", (), {"public_id": "model-a"})()
        ingresses = ()

    value = build(Profile(), {}, "digest")
    assert value["engine"] == "sglang"
    assert value["gateway"] == "kong"


def test_identity_has_fixed_dimensions_and_reports_non_configuration_notes():
    class Profile:
        guide = "guide-a"
        model = type("Model", (), {"public_id": "model-a"})()
        ingresses = ()

    value = build(
        Profile(), {"accelerator": "amd", "provider": "acme", "trigger": "nightly"}, "sha256:p"
    )
    assert set(value) == {
        "guide",
        "accelerator",
        "engine",
        "provider",
        "gateway",
        "model",
        "llm_d_ref",
        "llmdv_version",
        "profile_digest",
        "trigger",
    }
    assert differences(value, {**value, "llm_d_ref": "guide-b"}) == ["llm_d_ref: None -> 'guide-b'"]
