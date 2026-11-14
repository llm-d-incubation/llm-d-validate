"""What a client puts in its messages reaches the model, in every shape the API allows.

Newer SDKs send `developer` in place of `system`, most SDK helpers send content as an array of
text parts, and non-English users send multi-byte text. A layer that drops a role or a part,
or splits a character between chunks, returns a well-formed 200 with the wrong answer.
"""

from __future__ import annotations

import pytest

from ...assertions import fail
from ...evidence import journal
from ..conftest import budget_for, check_basic_completion, check_completion
from ..openai_contract import assemble_stream, require_contract, stream_frames

pytestmark = pytest.mark.surface("chat_completions")

UPPERCASE = "Answer in uppercase only."
UNICODE = "\u6f22\u5b57 \u0627\u0644\u0639\u0631\u0628\u064a\u0629 \U0001f680 e\u0301"


def _require_words(text: str, words: tuple[str, ...], why: str) -> None:
    missing = [word for word in words if word not in text]
    if missing:
        fail("instruction_reached_model", {"text": text, "missing": missing}, why)
    journal.record("instruction_reached_model", text, why, ok=True)


@pytest.mark.case_id("openai_chat.developer_role")
def test_chat_developer_role(client, surface, model, ingress):
    require_contract(ingress, surface)
    body = surface.minimal_body(model.public_id, budget_for(model), "Say: hello world")
    body["messages"].insert(0, {"role": "developer", "content": UPPERCASE})
    completion = check_basic_completion(
        client.post(surface.path, json_body=body), surface, model, ingress
    )
    _require_words(completion.text, ("HELLO",), "the developer instruction applied: HELLO")


@pytest.mark.case_id("openai_chat.content_parts")
def test_chat_content_parts(client, surface, model, ingress):
    """System and user content as text parts: every part reaches the model."""
    require_contract(ingress, surface)
    body = surface.minimal_body(model.public_id, budget_for(model))
    body["messages"] = [
        {"role": "system", "content": [{"type": "text", "text": UPPERCASE}]},
        {
            "role": "user",
            "content": [{"type": "text", "text": "Say: hello"}, {"type": "text", "text": " world"}],
        },
    ]
    completion = check_basic_completion(
        client.post(surface.path, json_body=body), surface, model, ingress
    )
    _require_words(completion.text, ("HELLO", "WORLD"), "both user parts, upper-cased by system")


@pytest.mark.case_id("openai_chat.unicode_round_trip")
@pytest.mark.parametrize("stream", [False, True], ids=["json", "sse"])
def test_chat_unicode_round_trip(client, surface, model, ingress, stream):
    """CJK, Arabic, an emoji and a combining mark come back intact, with no U+FFFD."""
    require_contract(ingress, surface)
    prompt = f"Repeat exactly, nothing else: {UNICODE}"
    if stream:
        body = surface.stream_body(model.public_id, budget_for(model), True, prompt=prompt)
        completion = assemble_stream(surface, stream_frames(client, surface, body, ingress))
        check_completion(completion, surface, model)
    else:
        body = surface.minimal_body(model.public_id, budget_for(model), prompt)
        completion = check_basic_completion(
            client.post(surface.path, json_body=body), surface, model, ingress
        )
    text = completion.text
    intact = "\u6f22\u5b57" in text and "\U0001f680" in text and "\ufffd" not in text
    if not intact:
        fail("unicode_round_trip", text, "the CJK and emoji intact, no replacement character")
    journal.record("unicode_round_trip", text, "intact, no replacement character", ok=True)
