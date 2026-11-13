"""Response timestamps, the input token count and conversation compaction."""

from __future__ import annotations

import pytest

from ...assertions import (
    assert_answer_only,
    assert_client_error,
    assert_error_envelope,
    assert_has_answer,
    assert_not_throttled,
    fail,
)
from ...evidence import journal
from ..conftest import budget_for, check_completion, check_json_response, nonce_document

pytestmark = pytest.mark.surface("responses")

COUNT_PATH = "/input_tokens"


@pytest.mark.case_id("openai_responses.completed_at")
def test_completed_response_has_a_completion_time(client, surface, model, ingress):
    """A completed response carries `completed_at`, no earlier than `created_at`."""
    body = surface.minimal_body(model.public_id, max_tokens=budget_for(model))
    response = check_json_response(client.post(surface.path, json_body=body), ingress)
    completion = surface.parse_completion(response)
    check_completion(completion, surface, model)
    if response.get("status") != "completed":
        pytest.skip(f"[requires_load] status {response.get('status')!r}; needs a completed answer")
    created, completed = response.get("created_at"), response.get("completed_at")
    ok = (
        isinstance(completed, (int, float))
        and not isinstance(completed, bool)
        and isinstance(created, (int, float))
        and completed >= created
    )
    journal.record("completed_at", completed, f"a timestamp at or after {created}", ok=ok)
    if not ok:
        fail("completed_at", completed, f"a timestamp at or after created_at {created}")


def _count(client, surface, model, words: int):
    text = nonce_document(words) if words else "Reply with the single word: ok"
    return client.post(
        surface.path + COUNT_PATH, json_body={"model": model.public_id, "input": text}
    )


@pytest.mark.case_id("openai_responses.input_tokens")
def test_input_token_count(client, surface, model, ingress):
    """Declared true: a count that grows with the input. False: a clean 4xx. Undeclared:
    either one, never a 5xx."""
    declared = ingress.capabilities.responses_input_tokens
    short = _count(client, surface, model, 0)
    assert_not_throttled(short, ingress)
    status = short.status
    clean = short.transport_error is None and status is not None and status < 500
    journal.record("input_tokens_status", status, "a 200 or a clean 4xx, never a 5xx", ok=clean)
    if not clean:
        fail("input_tokens_status", status, "a 200 or a clean 4xx, never a 5xx")
    if declared is False or (declared is None and status != 200):
        assert_client_error(short, ingress, "unsupported_feature")
        if declared is False:
            assert_error_envelope(short, surface, ingress, "unsupported_feature")
        return
    first = check_json_response(short, ingress)
    second = check_json_response(_count(client, surface, model, 200), ingress)
    counts = [first.get("input_tokens"), second.get("input_tokens")]
    objects = [first.get("object"), second.get("object")]
    ok = (
        objects == ["response.input_tokens"] * 2
        and all(type(c) is int and c > 0 for c in counts)
        and counts[1] > counts[0]
    )
    journal.record("input_token_counts", counts, "positive, larger for the longer input", ok=ok)
    if not ok:
        fail("input_token_counts", {"objects": objects, "counts": counts}, "growing counts")


HISTORY = [
    {"role": "user", "content": "Remember the word basalt."},
    {
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "id": "msg_llmdv_history",
        "content": [{"type": "output_text", "text": "Noted: basalt.", "annotations": []}],
    },
    {"role": "user", "content": "Reply with the single word: ok"},
]


@pytest.mark.case_id("openai_responses.compact")
def test_compact_endpoint(client, surface, model, ingress):
    """Declared true: user messages followed by one compaction item. False: a clean 4xx.
    Undeclared: either one, never a 5xx."""
    declared = ingress.capabilities.responses_compact
    exchange = client.post(
        surface.path + "/compact", json_body={"model": model.public_id, "input": HISTORY}
    )
    assert_not_throttled(exchange, ingress)
    status = exchange.status
    clean = exchange.transport_error is None and status is not None and status < 500
    journal.record("compact_status", status, "a 200 or a clean 4xx, never a 5xx", ok=clean)
    if not clean:
        fail("compact_status", status, "a 200 or a clean 4xx, never a 5xx")
    if declared is False or (declared is None and status != 200):
        assert_client_error(exchange, ingress, "unsupported_feature")
        if declared is False:
            assert_error_envelope(exchange, surface, ingress, "unsupported_feature")
        return
    body = check_json_response(exchange, ingress)
    output = body.get("output") if isinstance(body.get("output"), list) else []
    kinds = [item.get("type") if isinstance(item, dict) else None for item in output]
    usage = body.get("usage") if isinstance(body.get("usage"), dict) else {}
    ok = (
        body.get("object") == "response.compaction"
        and kinds[-1:] == ["compaction"]
        and all(kind == "message" for kind in kinds[:-1])
        and all(type(usage.get(key)) is int for key in ("input_tokens", "total_tokens"))
    )
    observed = {"object": body.get("object"), "output": kinds, "usage": sorted(usage)}
    journal.record("compaction", observed, "user messages then one compaction item", ok=ok)
    if not ok:
        fail("compaction", observed, "object response.compaction ending in a compaction item")


@pytest.mark.case_id("openai_responses.compact_replay")
def test_compacted_history_replays(client, surface, model, ingress):
    """Declared true: the compacted output, sent back with a new question, is read as history.

    User messages survive compaction verbatim, so the code word in one must reach the answer.
    """
    if ingress.capabilities.responses_compact is not True:
        pytest.skip(f"[undeclared] ingress {ingress.id!r} does not declare responses_compact")
    history = [
        {"role": "user", "content": "The code word is basalt. Remember it."},
        *HISTORY[1:],
    ]
    exchange = client.post(
        surface.path + "/compact", json_body={"model": model.public_id, "input": history}
    )
    compacted = check_json_response(exchange, ingress).get("output")
    if not isinstance(compacted, list) or not compacted:
        fail("compaction", compacted, "a non-empty output list")
    journal.record("compacted_items", [i.get("type") for i in compacted], "items to replay")
    question = {"role": "user", "content": "What is the code word? Reply with the code word only."}
    budget = budget_for(model, floor=128)
    body = surface.minimal_body(model.public_id, budget, prompt=[*compacted, question], store=False)
    response = check_json_response(client.post(surface.path, json_body=body), ingress)
    completion = surface.parse_completion(response)
    check_completion(completion, surface, model)
    assert_has_answer(completion, budget)
    assert_answer_only(completion, model, "basalt")
