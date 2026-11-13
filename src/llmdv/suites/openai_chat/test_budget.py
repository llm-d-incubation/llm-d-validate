"""A declared output budget must leave room for an answer, not only reasoning."""

import pytest

from ..conftest import check_answer_budget

pytestmark = pytest.mark.surface("chat_completions")


@pytest.mark.case_id("openai_chat.answer_budget")
def test_chat_answer_budget(client, surface, model, ingress):
    check_answer_budget(client, surface, model, ingress)
