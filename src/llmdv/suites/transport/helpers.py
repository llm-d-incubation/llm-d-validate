"""Shared pieces of the transport suite: the request that carries a transport probe."""

from __future__ import annotations

from typing import Any

import pytest

from ... import surfaces
from ...profile.schema import Ingress, Model
from ...surfaces.base import Surface
from ..conftest import budget_for, check_basic_completion

# The generation surface a transport probe rides on, in order of preference. HTTP behavior is a
# property of the layer, so one well-formed request is enough to carry it.
CARRIERS = ("chat_completions", "completions", "messages", "responses")


def carrier(ingress: Ingress, prefer: tuple[str, ...] = ()) -> Surface:
    """The first drivable generation surface the ingress exposes, or a classified skip."""
    for name in (*prefer, *CARRIERS):
        if ingress.exposes(name) and name in surfaces.implemented():
            return surfaces.get(name)
    exposed = [name for name in CARRIERS if ingress.exposes(name)]
    reason = "not_implemented" if exposed else "undeclared"
    pytest.skip(f"[{reason}] ingress {ingress.id!r} exposes no generation surface llmdv can drive")


def body(surface: Surface, model: Model, **extra: Any) -> dict[str, Any]:
    """A request that must be served with output, so it sends the surface's reply prompt."""
    extra.setdefault("prompt", surface.reply_prompt)
    return surface.minimal_body(model.public_id, max_tokens=budget_for(model), **extra)


def check_served(exchange, surface: Surface, model: Model, ingress: Ingress):
    """The probe was served as an ordinary generation."""
    return check_basic_completion(exchange, surface, model, ingress)
