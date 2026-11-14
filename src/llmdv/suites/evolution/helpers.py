"""Shared helpers for the evolution cases."""

from __future__ import annotations

import hashlib

from ...assertions import fail
from ..cache.helpers import check_unanswered_completion


def input_tokens(client, surface, model, ingress, **request) -> int:
    """The whole rendered input of one generation, cached part included.

    Messages reports cache reads and cache creation beside `input_tokens`, so a warm repeat
    would otherwise count less than a cold one for the same input. Only the input is measured,
    so the reply need carry no text: a raw prompt with no cue may end at once.
    """
    exchange = client.post(surface.path, **request)
    completion = check_unanswered_completion(exchange, surface, model, ingress)
    tokens = completion.usage.prompt_tokens
    if "cache_usage" in surface.features:
        total = surface.parse_cache_usage(exchange.response_body).input_tokens
        tokens = total if type(total) is int else tokens
    if not isinstance(tokens, int):
        fail("input_tokens", tokens, "an integer input token count")
    return tokens


def token_digest(token_ids: list[int]) -> str:
    """SHA-256 of the token ids written as decimal numbers joined by commas."""
    return hashlib.sha256(",".join(map(str, token_ids)).encode()).hexdigest()
