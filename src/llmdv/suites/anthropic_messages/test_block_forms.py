"""A string and a one-block list are the same content, so they must render the same prompt.

The API defines `"content": "x"` as shorthand for `[{"type": "text", "text": "x"}]`, and the same
for `system`. Clients differ in which form they send, and some switch forms between turns. A
layer that renders the two differently gives the same conversation two prompts, and a prefix
cached for one is a miss for the other.
"""

import pytest

from ...assertions.context import prompt_size
from ...evidence import journal
from ..conftest import budget_for, check_basic_completion

pytestmark = [
    pytest.mark.surface("messages"),
    pytest.mark.needs("messages_checks:block_forms"),
]

TEXT = "Reference text. " + "item " * 100 + "Reply with ok."
SYSTEM = "You are a helpful assistant. " + "rule " * 100


def _bodies(surface, model, part):
    budget = budget_for(model)
    if part == "user":
        plain = surface.minimal_body(model.public_id, budget, TEXT)
        blocks = surface.minimal_body(model.public_id, budget, TEXT)
        blocks["messages"][0]["content"] = [{"type": "text", "text": TEXT}]
        return plain, blocks
    plain = surface.minimal_body(model.public_id, budget, system=SYSTEM)
    blocks = surface.minimal_body(
        model.public_id, budget, system=[{"type": "text", "text": SYSTEM}]
    )
    return plain, blocks


@pytest.mark.case_id("anthropic_messages.block_form_same_prompt")
@pytest.mark.parametrize("part", ["user", "system"])
def test_string_and_block_forms_render_the_same_prompt(client, surface, model, ingress, part):
    sizes = [
        prompt_size(
            surface,
            check_basic_completion(
                client.post(surface.path, json_body=body), surface, model, ingress
            ),
        )
        for body in _bodies(surface, model, part)
    ]
    ok = sizes[0] == sizes[1]
    journal.record(
        f"{part}_block_form_tokens",
        {"string": sizes[0], "blocks": sizes[1]},
        "the same input tokens for both forms",
        ok=ok,
    )
    assert ok, f"{part} content as a string and as one text block rendered {sizes} tokens"
