"""Stream timing, cancel and buffering cases against paced, stalled and leaky fakes."""

from __future__ import annotations

import pytest

from tests.fakes import FakeFleet
from tests.transport_helpers import exchanges, outcomes, run

CHAT = {"surfaces": ("chat_completions",), "responses": False}
BUDGET = {"first_event_s": 1.0, "max_gap_s": 0.5, "min_tokens_per_s": 1}


@pytest.mark.parametrize(
    "fake,passes",
    [
        ({"stream_gap_s": 0.05}, True),
        ({"stream_gap_s": 0.05, "defects": {"stall"}}, False),
        ({"first_event_delay_s": 1.5}, False),
    ],
)
def test_steady_flow(tmp_path, fake, passes):
    with FakeFleet(transport=fake) as fleet:
        code, by_case = run(
            tmp_path,
            fleet,
            "test_stream_flows_within_budget",
            transport={"stream_budget": BUDGET},
            **CHAT,
        )
    chat = [r for r in by_case["streaming.steady_flow"] if r.get("variant") == "chat_completions"]
    assert [r["outcome"] for r in chat] == ["pass" if passes else "fail"]
    assert code == (0 if passes else 1)


def test_steady_flow_checks_completions(tmp_path):
    with FakeFleet(transport={"stream_gap_s": 0.05}) as fleet:
        code, by_case = run(
            tmp_path,
            fleet,
            "test_stream_flows_within_budget",
            transport={"stream_budget": BUDGET},
            surfaces=("chat_completions", "completions"),
            responses=False,
        )
    completions = [r for r in by_case["streaming.steady_flow"] if r.get("variant") == "completions"]
    chat = [r for r in by_case["streaming.steady_flow"] if r.get("variant") == "chat_completions"]
    assert len(completions) == 1
    assert completions[0]["outcome"] == "pass"
    assert [r["outcome"] for r in chat] == ["pass"]
    assert code == 0
    assert any("/v1/completions" in item["url"] for item in exchanges(tmp_path))


def test_steady_flow_skips_without_a_budget(tmp_path):
    with FakeFleet() as fleet:
        code, by_case = run(tmp_path, fleet, "test_stream_flows_within_budget", **CHAT)
    reasons = [r["reason"] for r in by_case["streaming.steady_flow"]]
    assert {r["skip_class"] for r in by_case["streaming.steady_flow"]} == {"undeclared"}
    assert any("stream_budget" in reason for reason in reasons)
    assert code == 0


@pytest.mark.parametrize("defects,passes", [(set(), True), ({"wedge_after_disconnect"}, False)])
def test_disconnect_before_output(tmp_path, defects, passes):
    with FakeFleet(transport={"stream_gap_s": 0.2, "defects": defects}) as fleet:
        code, by_case = run(tmp_path, fleet, "test_disconnect_before_output", **CHAT)
    (record,) = by_case["streaming.disconnect_before_output"]
    assert record["outcome"] == ("pass" if passes else "fail"), record
    closed = [c for c in record["checks"] if c["name"] == "closed_on_headers"]
    assert closed and closed[0]["ok"]
    assert code == (0 if passes else 1)


@pytest.mark.parametrize("defects,passes", [(set(), True), ({"slow_after_disconnect"}, False)])
def test_cancel_then_next(tmp_path, defects, passes):
    with FakeFleet(transport={"stream_gap_s": 0.2, "defects": defects}) as fleet:
        code, by_case = run(
            tmp_path,
            fleet,
            "test_cancel_then_next",
            transport={"stream_budget": {"next_request_s": 1.0}},
            **CHAT,
        )
    assert outcomes(by_case, "streaming.cancel_then_next") == {"pass" if passes else "fail"}
    assert code == (0 if passes else 1)


def test_cancellation_probes_send_messages_surface_headers(tmp_path):
    with FakeFleet(transport={"stream_gap_s": 0.2}) as fleet:
        code, by_case = run(
            tmp_path,
            fleet,
            "test_disconnect_before_output or test_cancel_then_next",
            transport={"stream_budget": {"next_request_s": 1.0}},
            surfaces=("messages",),
            responses=False,
        )
    assert outcomes(by_case, "streaming.disconnect_before_output") == {"pass"}
    assert outcomes(by_case, "streaming.cancel_then_next") == {"pass"}
    messages = [item for item in exchanges(tmp_path) if "/v1/messages" in item["url"]]
    assert messages
    assert all("anthropic-version" in item["request_headers"] for item in messages)
    assert code == 0


@pytest.mark.parametrize("defects,passes", [(set(), True), ({"buffer_compressed_stream"}, False)])
def test_stream_encoding(tmp_path, defects, passes):
    with FakeFleet(transport={"stream_gap_s": 0.05, "defects": defects}) as fleet:
        code, by_case = run(tmp_path, fleet, "test_stream_offered_gzip_is_not_buffered", **CHAT)
    assert outcomes(by_case, "transport.stream_encoding") == {"pass" if passes else "fail"}
    assert code == (0 if passes else 1)


def test_stream_encoding_skips_when_nothing_is_paced(tmp_path):
    with FakeFleet() as fleet:
        code, by_case = run(tmp_path, fleet, "test_stream_offered_gzip_is_not_buffered", **CHAT)
    (record,) = by_case["transport.stream_encoding"]
    assert record["outcome"] == "skip" and record["skip_class"] == "requires_load"
    assert code == 0
