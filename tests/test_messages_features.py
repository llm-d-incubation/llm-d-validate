"""Named Messages features against a fleet that serves, refuses or quietly mishandles them."""

from __future__ import annotations

import base64
import json

import pytest
import yaml

from llmdv.profile.loader import ProfileError, load
from llmdv.profile.schema import MESSAGES_CHECKS, MESSAGES_FEATURES
from llmdv.suites.anthropic_messages.features import OK_PROMPT, PNG, UNREACHABLE_FIXTURE
from tests.fakes import FakeFleet
from tests.test_suites_against_fake import run_suites, write_profile

FEATURE = "anthropic_messages.request_feature"
COUNT = "anthropic_messages.count_tokens_feature"
CONTEXT = 4096


def declare(
    tmp_path,
    fleet,
    features=None,
    checks=(),
    fleet_policy=None,
    rate_limited=False,
    **capabilities,
):
    path = write_profile(
        tmp_path,
        fleet,
        max_model_len=CONTEXT,
        fleet_policy=fleet_policy,
        rate_limited=rate_limited,
    )
    raw = yaml.safe_load(path.read_text())
    raw["capabilities"].update(capabilities)
    raw["capabilities"]["messages_features"] = features or {}
    raw["capabilities"]["messages_checks"] = [c for c in MESSAGES_CHECKS if c in checks]
    path.write_text(yaml.safe_dump(raw))
    return path


def run(
    tmp_path,
    fleet,
    select,
    features=None,
    checks=(),
    fleet_policy=None,
    rate_limited=False,
    **capabilities,
):
    profile = declare(
        tmp_path,
        fleet,
        features,
        checks,
        fleet_policy,
        rate_limited,
        **capabilities,
    )
    code, records = run_suites(profile, tmp_path / "run", "-k", select, "--llmdv-evidence", "all")
    return code, records


def by_variant(records, case):
    """The declared variants of a case; undeclared ones skip without a request."""
    return {
        r["variant"]: r
        for r in records
        if r.get("case_id") == case and r.get("skip_class") != "undeclared"
    }


def failed(record, name):
    return any(c["name"] == name and not c["ok"] for c in record["checks"])


def sent(tmp_path):
    lines = (tmp_path / "run" / "exchanges.jsonl").read_text().splitlines()
    return [json.loads(line) for line in lines]


# An image hosted online, which the fake serves itself.
FIXTURE = "https://images.llmdv.test/llmdv.png"
HOSTED = {FIXTURE: base64.b64decode(PNG)}
FIXTURE_CAPS = {"image_url_fixture": FIXTURE}


def fake(**anthropic):
    return FakeFleet(max_model_len=CONTEXT, anthropic=anthropic)


def test_every_feature_served_by_a_faithful_fleet_passes(tmp_path):
    with fake(hosted=HOSTED) as fleet:
        code, records = run(
            tmp_path,
            fleet,
            "test_request_feature",
            dict.fromkeys(MESSAGES_FEATURES, "accepted"),
            **FIXTURE_CAPS,
        )
    variants = by_variant(records, FEATURE)
    assert len(variants) == len(MESSAGES_FEATURES), sorted(variants)
    assert code == 0, {v: r["outcome"] for v, r in variants.items()}
    for record in variants.values():
        assert record["outcome"] == "pass" and record["checks"], record
    assert len(sent(tmp_path)) == len(MESSAGES_FEATURES)


def test_undeclared_features_send_nothing(tmp_path):
    with fake() as fleet:
        code, records = run(tmp_path, fleet, "test_request_feature or test_token_count_feature")
        assert not [s for s in fleet.seen if s[0] == "POST"]
    assert code == 0
    assert {r["skip_class"] for r in records} == {"undeclared"}


@pytest.mark.parametrize(
    "feature,rejects,defect,check",
    [
        ("document_text", (), "drops_blocks", "feature_content_reached_model"),
        ("search_result", (), "drops_blocks", "feature_content_reached_model"),
        (
            "server_tool_history",
            (),
            "drops_server_tool_history",
            "feature_content_reached_model",
        ),
        ("assistant_prefill", (), "restarts_prefill", "prefill_continued"),
        ("max_tokens_zero", (), "overruns_max_tokens", "cache_prewarm"),
        ("container", ("container",), None, "http_status"),
    ],
)
def test_an_accepted_feature_must_be_served(tmp_path, feature, rejects, defect, check):
    with fake(rejects=rejects, defect=defect) as fleet:
        code, records = run(tmp_path, fleet, "test_request_feature", {feature: "accepted"})
    (record,) = by_variant(records, FEATURE).values()
    assert code == 1 and record["outcome"] == "fail", record
    assert failed(record, check), record["checks"]


@pytest.mark.parametrize(
    "defect,check",
    [
        (None, None),
        ("server_error_on_reject", "rejection_status"),
        ("openai_error_shape", "error_envelope"),
        ("wrong_error_type", "anthropic_error_type"),
    ],
)
def test_a_rejected_feature_must_be_refused_cleanly(tmp_path, defect, check):
    with fake(rejects=("document",), defect=defect) as fleet:
        code, records = run(tmp_path, fleet, "test_request_feature", {"document_pdf": "rejected"})
    (record,) = by_variant(records, FEATURE).values()
    assert record["outcome"] == ("pass" if check is None else "fail"), record
    assert code == (0 if check is None else 1)
    if check:
        assert failed(record, check), record["checks"]
    (exchange,) = sent(tmp_path)
    assert exchange["request_body"]["messages"][0]["content"][0]["type"] == "document"


def test_an_image_url_is_fetched_from_the_declared_fixture(tmp_path):
    with fake(hosted=HOSTED) as fleet:
        code, records = run(
            tmp_path, fleet, "test_request_feature", {"image_url": "accepted"}, **FIXTURE_CAPS
        )
    (record,) = by_variant(records, FEATURE).values()
    assert code == 0 and record["outcome"] == "pass", record
    (exchange,) = sent(tmp_path)
    source = exchange["request_body"]["messages"][0]["content"][0]["source"]
    assert source == {"type": "url", "url": FIXTURE}


def test_an_image_url_without_a_fixture_needs_instrumentation(tmp_path):
    with fake(hosted=HOSTED) as fleet:
        code, records = run(tmp_path, fleet, "test_request_feature", {"image_url": "accepted"})
        assert not [s for s in fleet.seen if s[0] == "POST"]
    (record,) = [r for r in records if r.get("case_id") == FEATURE and r["variant"] == "image_url"]
    assert code == 0 and record["outcome"] == "skip", record
    assert record["skip_class"] == "requires_instrumentation", record
    assert "image_url_fixture" in record["reason"], record


@pytest.mark.parametrize(
    "config,fixture",
    [
        # The deployment refuses the url source, or crashes on it.
        ({"hosted": HOSTED, "rejects": ("url_image",)}, FIXTURE),
        ({"hosted": HOSTED, "defect": "url_image_5xx"}, FIXTURE),
        # The fixture cannot be fetched: gone from its host, or a host that does not resolve.
        ({"hosted": HOSTED}, FIXTURE.replace("llmdv.png", "gone.png")),
        ({"hosted": HOSTED}, "https://images.example.invalid/llmdv.png"),
    ],
)
def test_an_accepted_image_url_that_is_not_served_fails(tmp_path, config, fixture):
    with fake(**config) as fleet:
        code, records = run(
            tmp_path,
            fleet,
            "test_request_feature",
            {"image_url": "accepted"},
            image_url_fixture=fixture,
        )
    (record,) = by_variant(records, FEATURE).values()
    assert code == 1 and record["outcome"] == "fail", record
    assert failed(record, "http_status"), record["checks"]


@pytest.mark.parametrize(
    "value", ["http://images.llmdv.test/llmdv.png", "images.llmdv.test/llmdv.png", "https://", 7]
)
def test_the_image_url_fixture_must_be_an_https_url(tmp_path, value):
    with fake() as fleet:
        path = declare(tmp_path, fleet, image_url_fixture=value)
    with pytest.raises(ProfileError, match="image_url_fixture"):
        load(path)


def test_a_rejected_image_url_needs_no_fixture(tmp_path):
    """A refusal is proved by any URL, so a declared rejection runs without a fixture."""
    with fake(rejects=("url_image",)) as fleet:
        code, records = run(tmp_path, fleet, "test_request_feature", {"image_url": "rejected"})
    (record,) = by_variant(records, FEATURE).values()
    assert code == 0 and record["outcome"] == "pass", record
    (exchange,) = sent(tmp_path)
    source = exchange["request_body"]["messages"][0]["content"][0]["source"]
    assert source == {"type": "url", "url": UNREACHABLE_FIXTURE}


def test_a_rejected_image_url_is_sent_the_declared_fixture(tmp_path):
    """With a fixture, a 4xx proves the refusal is a policy, not a fetch that failed."""
    with fake(hosted=HOSTED, rejects=("url_image",)) as fleet:
        code, records = run(
            tmp_path, fleet, "test_request_feature", {"image_url": "rejected"}, **FIXTURE_CAPS
        )
    (record,) = by_variant(records, FEATURE).values()
    assert code == 0 and record["outcome"] == "pass", record
    (exchange,) = sent(tmp_path)
    source = exchange["request_body"]["messages"][0]["content"][0]["source"]
    assert source == {"type": "url", "url": FIXTURE}


def test_a_rejected_image_url_whose_fixture_is_served_fails(tmp_path):
    """A deployment that fetches the declared image no longer refuses the url source.

    It answers a failed fetch with a 4xx, as a fetch's own 404 can be: sent an unreachable URL
    instead of the fixture, it would read as a refusal and pass.
    """
    fetch_4xx = {**HOSTED, UNREACHABLE_FIXTURE.replace("llmdv.png", "other.png"): HOSTED[FIXTURE]}
    with fake(hosted=fetch_4xx) as fleet:
        code, records = run(
            tmp_path, fleet, "test_request_feature", {"image_url": "rejected"}, **FIXTURE_CAPS
        )
    (record,) = by_variant(records, FEATURE).values()
    assert code == 1 and record["outcome"] == "fail", record
    assert failed(record, "rejection_status"), record["checks"]


@pytest.mark.parametrize(
    "config",
    [
        {},  # the url source is served
        {"hosted": HOSTED},  # the deployment tries the fetch and fails with a 500
    ],
)
def test_a_rejected_feature_that_is_served_fails(tmp_path, config):
    with fake(**config) as fleet:
        code, records = run(tmp_path, fleet, "test_request_feature", {"image_url": "rejected"})
    (record,) = by_variant(records, FEATURE).values()
    assert code == 1 and record["outcome"] == "fail", record
    assert failed(record, "rejection_status"), record["checks"]


@pytest.mark.parametrize(
    "defect,passes", [(None, True), ("count_5xx_on_blocks", False), ("drops_blocks", True)]
)
def test_count_tokens_counts_a_served_feature(tmp_path, defect, passes):
    with fake(defect=defect) as fleet:
        code, records = run(
            tmp_path,
            fleet,
            "test_token_count_feature",
            {"document_text": "accepted", "image_base64": "accepted"},
        )
    variants = by_variant(records, COUNT)
    assert len(variants) == 2
    assert code == (0 if passes else 1), variants
    assert all(r["outcome"] == ("pass" if passes else "fail") for r in variants.values())
    bodies = [e["request_body"] for e in sent(tmp_path)]
    assert all("max_tokens" not in body for body in bodies)


@pytest.mark.parametrize(
    "config,passes",
    [
        # A 1x1 PNG counted at one token, fewer than the words QUESTION has over OK_PROMPT: a
        # baseline of another prompt reads that as an image not counted.
        ({"image_tokens": 1}, True),
        ({"image_tokens": 1, "defect": "count_ignores_images"}, False),
    ],
)
def test_count_tokens_compares_a_feature_with_its_own_prompt(tmp_path, config, passes):
    with fake(**config) as fleet:
        code, records = run(
            tmp_path, fleet, "test_token_count_feature", {"image_base64": "accepted"}
        )
    (record,) = by_variant(records, COUNT).values()
    assert code == (0 if passes else 1), record
    assert record["outcome"] == ("pass" if passes else "fail"), record
    assert failed(record, "feature_counted") is not passes, record["checks"]
    feature, baseline = (e["request_body"] for e in sent(tmp_path))
    assert baseline["messages"] == [{"role": "user", "content": OK_PROMPT}]
    assert feature["messages"][0]["content"][-1] == {"type": "text", "text": OK_PROMPT}


def test_a_feature_request_carries_its_beta_value(tmp_path):
    with fake() as fleet:
        run(tmp_path, fleet, "test_request_feature", {"speed_fast": "accepted"})
    (exchange,) = sent(tmp_path)
    assert exchange["request_body"]["speed"] == "fast"
    assert exchange["request_headers"]["anthropic-beta"] == "fast-mode-2026-02-01"


def test_the_pdf_feature_sends_a_parseable_pdf_holding_the_marker(tmp_path):
    import base64

    with fake() as fleet:
        run(tmp_path, fleet, "test_request_feature", {"document_pdf": "accepted"})
    (exchange,) = sent(tmp_path)
    source = exchange["request_body"]["messages"][0]["content"][0]["source"]
    pdf = base64.b64decode(source["data"])
    assert pdf.startswith(b"%PDF-1.4") and pdf.rstrip().endswith(b"%%EOF")
    offset = int(pdf.rsplit(b"startxref\n", 1)[1].split()[0])
    assert pdf[offset:].startswith(b"xref")


def test_server_tool_history_holds_its_marker_in_the_hosted_call(tmp_path):
    with fake() as fleet:
        run(tmp_path, fleet, "test_request_feature", {"server_tool_history": "accepted"})
    (exchange,) = sent(tmp_path)
    assistant = exchange["request_body"]["messages"][1]["content"]
    call = next(block for block in assistant if block["type"] == "server_tool_use")
    result = next(block for block in assistant if block["type"] == "web_search_tool_result")
    marker = call["input"]["query"]
    assert marker.startswith("The code word is llmdv")
    assert result["content"] == []
    assert not [block for block in assistant if block["type"] == "text"]


@pytest.mark.parametrize(
    "declared,message",
    [({"not_a_feature": "accepted"}, "unknown feature"), ({"top_p": "maybe"}, "accepted or")],
)
def test_loader_rejects_unknown_features_and_outcomes(tmp_path, declared, message):
    with fake() as fleet:
        path = declare(tmp_path, fleet, declared)
    with pytest.raises(ProfileError, match=message):
        load(path)
