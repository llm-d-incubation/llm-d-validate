"""A render result is a typed engine input, not a completion."""

from __future__ import annotations

from ..evidence import journal
from ..profile.schema import Model
from ..surfaces.render import RenderedPrompt


def assert_rendered(prompt: RenderedPrompt, model: Model) -> None:
    values = prompt.token_ids
    ok = (
        isinstance(values, list)
        and bool(values)
        and all(type(value) is int and value >= 0 for value in values)
    )
    journal.record("render_token_ids", values, "non-empty non-negative integer token IDs", ok=ok)
    assert ok, "render returned no valid token IDs"
    ok = prompt.model in model.all_names
    journal.record("render_model", prompt.model, model.all_names, ok=ok)
    assert ok, "render returned another model"
    ok = isinstance(prompt.request_id, str) and bool(prompt.request_id)
    journal.record("render_request_id", prompt.request_id, "non-empty ID", ok=ok)
    assert ok, "render returned no request ID"
    ok = isinstance(prompt.sampling_params, dict)
    journal.record(
        "render_sampling_params", prompt.sampling_params, "a sampling-parameter object", ok=ok
    )
    assert ok, "render returned no sampling parameters"
