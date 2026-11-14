"""The pinned SDK's structured-output helpers, end to end.

`chat.completions.parse` and `chat.completions.stream` send a strict JSON schema built from a
Pydantic model and parse the reply into that model. They raise when the content is not JSON
for the model, so a hop that drops `response_format`, or reasoning left in `content`, breaks
every client built on them.
"""

from __future__ import annotations

import pytest

from ...assertions import fail
from ...assertions.structured_output import assert_structured_reasoning
from ...evidence import journal
from ..conftest import budget_for, throttle_aware
from ..openai_contract import require_contract

pytestmark = pytest.mark.surface("chat_completions")

PROMPT = "What is 17 times 23? Reply in the requested JSON format."


@pytest.mark.needs("structured_output")
@pytest.mark.case_id("openai_chat.sdk_parse_structured")
@pytest.mark.parametrize("stream", [False, True], ids=["json", "sse"])
def test_chat_sdk_parse_structured(openai_client, surface, model, ingress, stream):
    """The SDK parses the reply into the requested model, streamed or not."""
    require_contract(ingress, surface)
    openai = pytest.importorskip("openai")
    pydantic = pytest.importorskip("pydantic")

    class Answer(pydantic.BaseModel):
        answer: int

    request = {
        "model": model.public_id,
        "messages": [{"role": "user", "content": PROMPT}],
        "response_format": Answer,
        "max_tokens": budget_for(model, floor=256),
        "temperature": 0,
    }
    journal.record("sdk_helper", "stream" if stream else "parse")
    try:
        with throttle_aware(ingress):
            if stream:
                with openai_client.chat.completions.stream(**request) as events:
                    final = events.get_final_completion()
            else:
                final = openai_client.chat.completions.parse(**request)
    except openai.APIStatusError as exc:
        fail("sdk_status", exc.status_code, "200")
    except (openai.LengthFinishReasonError, ValueError) as exc:
        fail("sdk_parse", f"{type(exc).__name__}: {str(exc)[:300]}", "a reply the SDK parses")
    parsed = final.choices[0].message.parsed
    journal.record(
        "sdk_parsed", parsed.model_dump() if parsed else None, "an Answer", ok=bool(parsed)
    )
    if parsed is None:
        fail("sdk_parsed", final.choices[0].message.content, "a reply the SDK parses")
    if not stream:
        # The stream helper sends no include_usage and keeps only the fields it knows, so only
        # the parsed reply shows whether the model reasoned.
        assert_structured_reasoning(surface.parse_completion(final.to_dict()), model)
