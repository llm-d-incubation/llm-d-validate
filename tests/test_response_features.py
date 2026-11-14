import base64
import struct
import zlib

import pytest
import yaml

from llmdv.profile.loader import ProfileError, load
from llmdv.profile.schema import RESPONSES_FEATURES, RESPONSES_REFERENCE_FEATURES
from llmdv.surfaces.responses_features import FEATURES
from tests.fakes import FakeFleet, FakeResponsesConfig, reasoning_only_response
from tests.test_suites_against_fake import run_suites, write_profile

TOOLS = ("web_search", "mcp", "shell")
ECHOED = ("temperature", "parallel_tool_calls", "prompt_cache_key", "text_verbosity")
EFFECTS = ("reasoning_summary", "encrypted_reasoning", "input_file", "stream_obfuscation_off")


def run(tmp_path, features, declared, mode, *, response_overrides=None):
    with FakeFleet(
        response_config=FakeResponsesConfig(feature_mode=mode),
        response_overrides=response_overrides,
        transport={"multimodal": True},
    ) as fleet:
        path = write_profile(tmp_path, fleet, responses=True)
        raw = yaml.safe_load(path.read_text())
        raw["capabilities"] = {"responses_features": dict.fromkeys(features, declared)}
        if declared is None:
            raw["capabilities"] = {}
        path.write_text(yaml.safe_dump(raw))
        code, records = run_suites(path, tmp_path / "run", "-k", "test_responses_request_feature")
    by_feature = {r["nodeid"].rstrip("]").rsplit("-", 1)[1]: r for r in records}
    assert set(by_feature) == set(RESPONSES_FEATURES), by_feature
    return code, {name: by_feature[name]["outcome"] for name in features}


def test_every_feature_has_a_wire_shape():
    assert tuple(FEATURES) == RESPONSES_FEATURES
    assert {n for n, f in FEATURES.items() if f.reference} == set(RESPONSES_REFERENCE_FEATURES)


def test_input_image_fixture_is_a_valid_png():
    body = FEATURES["input_image"].body("fixture-check")
    image_url = body["input"][0]["content"][1]["image_url"]
    encoded = image_url.removeprefix("data:image/png;base64,")
    data = base64.b64decode(encoded, validate=True)
    assert data.startswith(b"\x89PNG\r\n\x1a\n")

    chunks = []
    compressed = bytearray()
    offset = 8
    while offset < len(data):
        length = struct.unpack_from(">I", data, offset)[0]
        kind = data[offset + 4 : offset + 8]
        payload = data[offset + 8 : offset + 8 + length]
        checksum = struct.unpack_from(">I", data, offset + 8 + length)[0]
        assert checksum == zlib.crc32(kind + payload) & 0xFFFFFFFF
        chunks.append(kind)
        if kind == b"IDAT":
            compressed.extend(payload)
        offset += 12 + length

    assert offset == len(data)
    assert chunks == [b"IHDR", b"IDAT", b"IEND"]
    assert zlib.decompress(compressed)


@pytest.mark.parametrize(
    "declared,mode,outcome",
    [
        (None, None, "pass"),
        (None, "refuse", "pass"),
        (None, "ignore", "pass"),
        (None, "server_error", "fail"),
        ("rejected", "refuse", "pass"),
        ("rejected", "ignore", "fail"),
        ("ignored", "ignore", "pass"),
        ("ignored", "echo", "pass"),
        ("ignored", "refuse", "fail"),
        ("ignored", "server_error", "fail"),
    ],
)
def test_every_feature_follows_its_disposition(tmp_path, declared, mode, outcome):
    features = RESPONSES_FEATURES
    code, outcomes = run(tmp_path, features, declared, mode)
    assert set(outcomes.values()) == {outcome}, outcomes
    assert code == (1 if outcome == "fail" else 0)


@pytest.mark.parametrize(
    "features,mode,outcome",
    [
        (ECHOED + EFFECTS, "echo", "pass"),
        (ECHOED + EFFECTS, "wrong_echo", "fail"),
        # A fleet that never pads a stream honors obfuscation off by ignoring it.
        (ECHOED + EFFECTS[:-1], "ignore", "fail"),
        (ECHOED + EFFECTS, "refuse", "fail"),
    ],
)
def test_accepted_features_are_echoed_and_take_effect(tmp_path, features, mode, outcome):
    code, outcomes = run(tmp_path, features, "accepted", mode)
    assert set(outcomes.values()) == {outcome}, outcomes
    assert code == (1 if outcome == "fail" else 0)


def test_input_file_effect_requires_an_answer_when_reasoning_exhausts_the_budget(tmp_path):
    code, outcomes = run(
        tmp_path,
        ("input_file",),
        "accepted",
        "echo",
        response_overrides=reasoning_only_response(),
    )
    assert outcomes == {"input_file": "fail"}, outcomes
    assert code == 1


@pytest.mark.parametrize(
    "mode,outcome",
    [
        # The fake reports the sampling it applied: top_p 1.0 at temperature 0, as vLLM does.
        ("echo", "pass"),
        ("drops_sampling", "fail"),
        ("wrong_echo", "fail"),
    ],
)
def test_top_p_is_echoed_from_a_sampled_request(tmp_path, mode, outcome):
    assert FEATURES["top_p"].body("m")["temperature"] > 0
    code, outcomes = run(tmp_path, ("top_p",), "accepted", mode)
    assert outcomes == {"top_p": outcome}, outcomes
    assert code == (1 if outcome == "fail" else 0)


def test_an_ignored_tool_must_not_report_a_call(tmp_path):
    code, outcomes = run(tmp_path, TOOLS, "ignored", "phantom_call")
    assert set(outcomes.values()) == {"fail"}, outcomes
    assert code == 1


@pytest.mark.parametrize(
    "value,match",
    [
        ({"not_a_feature": "ignored"}, "unknown feature"),
        ({"temperature": "maybe"}, "expected one of"),
        ({"conversation": "accepted"}, "expected one of ignored, rejected"),
        (["temperature"], "expected a mapping"),
    ],
)
def test_feature_declarations_are_validated(tmp_path, value, match):
    path = tmp_path / "bad.yaml"
    path.write_text(yaml.safe_dump({"capabilities": {"responses_features": value}}))
    with pytest.raises(ProfileError, match=match):
        load(path)
