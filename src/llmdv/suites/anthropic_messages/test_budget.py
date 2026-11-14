"""A declared output budget must leave room for an answer, not only thinking."""

import pytest

from ..conftest import check_answer_budget

pytestmark = pytest.mark.surface("messages")


@pytest.mark.case_id("anthropic_messages.answer_budget")
def test_messages_answer_budget(client, surface, model, ingress):
    check_answer_budget(client, surface, model, ingress)
