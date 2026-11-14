"""The non-streaming completion contract a completion-only client depends on.

A draining replica can return finish_reason="abort" on HTTP 200. The shared finish check
must reject that response, even when it otherwise looks like a successful completion.
"""

from __future__ import annotations

import pytest

from ..conftest import budget_for, check_basic_completion

pytestmark = pytest.mark.surface("completions")


@pytest.mark.case_id("openai_completions.basic_non_streaming")
def test_completions_basic(client, surface, model, ingress):
    body = surface.minimal_body(model.public_id, budget_for(model), surface.reply_prompt)
    exchange = client.post(surface.path, json_body=body)
    check_basic_completion(exchange, surface, model, ingress)
