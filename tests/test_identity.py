import pytest

from llmdv.cli.compare_cmd import _identity_notes
from llmdv.identity import (
    CONFIGURATION_DIMENSIONS,
    RESERVED_LABELS,
    IdentityError,
    build,
    differences,
    parse,
    validate_lane,
)


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
        "flavor",
    }
    assert differences(value, {**value, "llm_d_ref": "guide-b"}) == ["llm_d_ref: None -> 'guide-b'"]


class _Profile:
    guide = "guide-a"
    model = type("Model", (), {"public_id": "model-a"})()
    ingresses = ()


def test_flavor_is_a_lane_dimension_recorded_in_identity():
    lane = parse(["accelerator=gpu", "flavor=lmcache-cpu"])
    assert lane == {"accelerator": "gpu", "flavor": "lmcache-cpu", "trigger": "manual"}
    value = build(_Profile(), lane, "sha256:p")
    assert value["flavor"] == "lmcache-cpu"
    validate_lane(lane, value)


def test_a_run_without_a_flavor_records_none():
    lane = parse(["accelerator=gpu"])
    assert "flavor" not in lane
    assert build(_Profile(), lane, "sha256:p")["flavor"] is None


def test_an_empty_flavor_is_a_usage_error():
    with pytest.raises(IdentityError, match="flavor"):
        parse(["flavor="])


def test_flavor_is_configuration_not_a_provenance_note():
    assert "flavor" in CONFIGURATION_DIMENSIONS
    assert "flavor" in RESERVED_LABELS
    value = build(_Profile(), parse(["flavor=lmcache-cpu"]), "sha256:p")
    other = {**value, "flavor": "native-fs"}
    assert differences(value, other) == ["flavor: 'lmcache-cpu' -> 'native-fs'"]
    # A configuration difference picks the baseline; it is never reported as a note.
    assert _identity_notes(value, other) == []
    assert _identity_notes(value, {**other, "llm_d_ref": "ref-b"}) == [
        "identity llm_d_ref: None -> 'ref-b'"
    ]


@pytest.mark.parametrize("value", ["flavor=lmcache-cpu ", "flavor= x", "flavor=x\n"])
def test_a_value_with_stray_whitespace_is_refused(value):
    """It would otherwise be a lane of its own, compared with no earlier run."""
    with pytest.raises(IdentityError, match="leading or trailing whitespace"):
        parse([value])


@pytest.mark.parametrize("value", ["flavor=", "flavor= ", "flavor=\n"])
def test_an_empty_or_blank_value_is_refused(value):
    with pytest.raises(IdentityError, match="must be one of key=value"):
        parse([value])


def test_a_later_value_replaces_an_earlier_one():
    """How a caller overrides the runner's default, e.g. `trigger=nightly` from the CronJob."""
    assert parse(["flavor=a", "flavor=b"])["flavor"] == "b"
