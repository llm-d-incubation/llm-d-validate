"""budget_for: the output budget a case asks with, so a reasoning model still answers."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from llmdv.suites.conftest import budget_for


def model(default_on: bool, answer_max_tokens: int | None) -> SimpleNamespace:
    return SimpleNamespace(
        reasoning=SimpleNamespace(default_on=default_on), answer_max_tokens=answer_max_tokens
    )


@pytest.mark.parametrize(
    "default_on,answer,floor,expected",
    [
        (False, None, 64, 64),
        (True, None, 64, 256),
        (True, None, 512, 2048),
        # A model measured to need more than the fixed floor gets its declared answer budget:
        # a long reasoner spends hundreds of tokens on a one-line instruction at its default effort.
        (True, 1024, 64, 1024),
        (False, 1024, 64, 1024),
        # A smaller declared budget never shrinks the floor.
        (True, 128, 64, 256),
        (True, 1024, 512, 2048),
    ],
)
def test_budget_for(default_on, answer, floor, expected):
    assert budget_for(model(default_on, answer), floor=floor) == expected
