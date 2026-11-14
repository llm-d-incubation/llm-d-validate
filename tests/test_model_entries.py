import pytest
import yaml

from llmdv.profile.loader import ProfileError, load
from tests.fakes import FakeFleet, FakeModelDiscoveryConfig
from tests.test_suites_against_fake import run_suites, write_profile


def run(tmp_path, selector, declared=None, name="model_retrieval", **flags):
    with FakeFleet(**flags) as fleet:
        path = write_profile(tmp_path, fleet)
        if declared is not None:
            raw = yaml.safe_load(path.read_text())
            raw.setdefault("capabilities", {})[name] = declared
            path.write_text(yaml.safe_dump(raw))
        code, records = run_suites(path, tmp_path / "run", "-k", selector)
    (record,) = records
    return code, record


@pytest.mark.parametrize("defect,outcome", [(None, "pass"), ("missing_fields", "fail")])
def test_model_entries(tmp_path, defect, outcome):
    code, record = run(
        tmp_path,
        "test_model_entries_have_typed_fields",
        model_discovery_config=FakeModelDiscoveryConfig(listing_defect=defect),
    )
    assert record["outcome"] == outcome, record
    assert code == (1 if outcome == "fail" else 0)


@pytest.mark.parametrize(
    "defect,outcome",
    [(None, "pass"), ("retrieve_any", "fail"), ("retrieve_server_error", "fail")],
)
def test_unknown_model_retrieval(tmp_path, defect, outcome):
    code, record = run(
        tmp_path,
        "test_unknown_model_retrieval_is_refused",
        model_discovery_config=FakeModelDiscoveryConfig(listing_defect=defect),
    )
    assert record["outcome"] == outcome, record


@pytest.mark.parametrize("defect", ["detail_body", "empty_body"])
def test_unknown_model_retrieval_error_body_is_reported(tmp_path, defect):
    code, record = run(
        tmp_path,
        "test_unknown_model_retrieval_is_refused",
        model_discovery_config=FakeModelDiscoveryConfig(listing_defect=defect),
    )
    assert record["outcome"] == "fail", record
    checks = [check for check in record["checks"] if check["name"] == "error_envelope"]
    assert checks and checks[0]["ok"] is False
    assert "error_envelope" in record["reason"]
    assert "TypeError" not in record["reason"]
    assert code == 1


@pytest.mark.parametrize(
    "declared,flags,outcome",
    [
        (None, {}, "skip"),
        (True, {"model_discovery_config": FakeModelDiscoveryConfig(serve_retrieval=True)}, "pass"),
        (True, {}, "fail"),
        (False, {}, "pass"),
        (
            False,
            {"model_discovery_config": FakeModelDiscoveryConfig(serve_retrieval=True)},
            "fail",
        ),
        (
            False,
            {"model_discovery_config": FakeModelDiscoveryConfig(listing_defect="redirect")},
            "fail",
        ),
    ],
)
def test_served_model_retrieval(tmp_path, declared, flags, outcome):
    code, record = run(tmp_path, "test_served_model_retrieval", declared, **flags)
    assert record["outcome"] == outcome, record
    if outcome == "skip":
        assert "undeclared" in record["reason"]


@pytest.mark.parametrize("defect", ["detail_body", "empty_body"])
def test_served_model_retrieval_error_body_is_reported(tmp_path, defect):
    code, record = run(
        tmp_path,
        "test_served_model_retrieval",
        declared=False,
        model_discovery_config=FakeModelDiscoveryConfig(listing_defect=defect),
    )
    assert record["outcome"] == "fail", record
    checks = [check for check in record["checks"] if check["name"] == "error_envelope"]
    assert checks and checks[0]["ok"] is False
    assert "error_envelope" in record["reason"]
    assert "TypeError" not in record["reason"]
    assert code == 1


def test_model_retrieval_declaration_is_a_boolean(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text(yaml.safe_dump({"capabilities": {"model_retrieval": "yes"}}))
    with pytest.raises(ProfileError, match="model_retrieval"):
        load(path)


DECLARED_INFO = {
    "mode": "chat",
    "supports_reasoning": True,
    "supports_vision": False,
    "max_input_tokens": 4096,
    "supported_endpoints": ["/v1/messages", "/v1/chat/completions"],
}
SERVED_INFO = {**DECLARED_INFO, "supported_endpoints": ["/v1/chat/completions", "/v1/messages"]}


def info(**changes):
    return {"model_discovery_config": FakeModelDiscoveryConfig(model_info=changes)}


@pytest.mark.parametrize(
    "declared,flags,outcome",
    [
        (None, info(**SERVED_INFO), "skip"),
        (DECLARED_INFO, info(**SERVED_INFO), "pass"),
        # A gateway that was never told the model's capabilities reports them as null.
        (DECLARED_INFO, info(**{**SERVED_INFO, "supports_reasoning": None}), "fail"),
        (DECLARED_INFO, info(**{**SERVED_INFO, "supports_vision": None}), "fail"),
        (DECLARED_INFO, info(**{**SERVED_INFO, "supports_vision": True}), "fail"),
        (DECLARED_INFO, info(**{**SERVED_INFO, "supports_reasoning": "true"}), "fail"),
        (DECLARED_INFO, info(**{**SERVED_INFO, "max_input_tokens": 8192}), "fail"),
        (DECLARED_INFO, info(**{**SERVED_INFO, "supported_endpoints": ["/v1/messages"]}), "fail"),
        (DECLARED_INFO, info(), "fail"),
        # The route is unserved, as on a model server.
        (DECLARED_INFO, {}, "fail"),
        (
            DECLARED_INFO,
            {
                "model_discovery_config": FakeModelDiscoveryConfig(
                    model_info=SERVED_INFO, model_info_name="another-model"
                )
            },
            "fail",
        ),
    ],
)
def test_model_info(tmp_path, declared, flags, outcome):
    code, record = run(
        tmp_path, "test_model_info_matches_declaration", declared, "model_info", **flags
    )
    assert record["outcome"] == outcome, record
    assert code == (1 if outcome == "fail" else 0)
    if outcome == "skip":
        assert "undeclared" in record["reason"]
    if outcome == "fail":
        assert any(check.get("ok") is False for check in record["checks"]), record


@pytest.mark.parametrize(
    "value",
    ["yes", ["supports_vision"], {"supports_vision": None}, {"supports_vision": {"a": 1}}],
)
def test_model_info_declaration_is_a_mapping_of_scalars_or_string_lists(tmp_path, value):
    path = tmp_path / "bad.yaml"
    path.write_text(yaml.safe_dump({"capabilities": {"model_info": value}}))
    with pytest.raises(ProfileError, match="model_info"):
        load(path)
