"""Requested logprobs stream with the text deltas and add up to the completed part."""

from __future__ import annotations

import pytest

from ...assertions import fail
from ...assertions.logprobs import assert_logprobs
from ...evidence import journal
from ...surfaces.responses import TERMINAL_EVENTS
from ..conftest import budget_for, check_completion, check_sse_response

pytestmark = [pytest.mark.surface("responses"), pytest.mark.needs("responses_options:logprobs")]

TOP = 2


@pytest.mark.case_id("openai_responses.stream_logprobs")
def test_streamed_logprobs(client, surface, model, ingress):
    """Every output_text delta carries a logprobs list; together they are the snapshot's."""
    body = surface.stream_body(
        model.public_id,
        max_tokens=budget_for(model, floor=256),
        include=["message.output_text.logprobs"],
        top_logprobs=TOP,
    )
    exchange, transcript = client.stream(
        surface.path, json_body=body, stop_event=TERMINAL_EVENTS | {"error"}
    )
    check_sse_response(exchange, ingress)
    surface.assert_stream(transcript)
    frames = [event.json() for event in transcript.data_events]
    deltas = [f for f in frames if f.get("type") == "response.output_text.delta"]
    missing = sum(not isinstance(f.get("logprobs"), list) for f in deltas)
    journal.record("delta_logprobs", missing, "a logprobs list on every text delta", ok=not missing)
    if not deltas or missing:
        fail(
            "delta_logprobs", {"deltas": len(deltas), "missing": missing}, "logprobs on every delta"
        )
    streamed = [entry for f in deltas for entry in f["logprobs"]]
    assert_logprobs([streamed], TOP)
    output = (frames[-1].get("response") or {}).get("output") or []
    parts = [
        part.get("logprobs")
        for item in output
        if isinstance(item, dict) and item.get("type") == "message"
        for part in item.get("content") or []
        if isinstance(part, dict) and part.get("type") == "output_text"
    ]
    completed = [entry for entries in parts if isinstance(entries, list) for entry in entries]
    ok = len(completed) == len(streamed)
    journal.record("logprobs_consistency", [len(streamed), len(completed)], "equal counts", ok=ok)
    if not ok:
        fail("logprobs_consistency", [len(streamed), len(completed)], "equal token counts")
    completion = surface.assemble([surface.parse_chunk(frame) for frame in frames])
    check_completion(completion, surface, model)
