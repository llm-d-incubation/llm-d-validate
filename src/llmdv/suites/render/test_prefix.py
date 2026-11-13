"""Synthetic volatile suffixes must preserve a declared token-prefix floor."""

import pytest

from ...assertions import fail
from ...assertions.prefix import assert_prefix_pair, common_prefix_length
from ...evidence import journal
from .helpers import render_request
from .prefix_fixtures import FIXTURES


@pytest.mark.needs("prefix_stability")
@pytest.mark.parametrize(
    "surface,fixture",
    [(item.surface, item) for item in FIXTURES],
    ids=[item.id for item in FIXTURES],
    indirect=["surface"],
)
@pytest.mark.case_id("render.synthetic_prefix")
def test_synthetic_prefix_stability(client, surface, model, ingress, fixture):
    base = surface.minimal_body(model.public_id)
    controls = [
        render_request(client, surface, model, ingress, {**base, **body})
        for body in fixture.controls
    ]
    minimum = common_prefix_length(*controls)
    if not minimum or controls[0] == controls[1]:
        fail(
            "prefix_stable_boundary",
            {"common": minimum, "identical": controls[0] == controls[1]},
            "distinct control renders with a non-empty common prefix",
        )
    journal.record(
        "prefix_fixture",
        {
            "id": fixture.id,
            "synthetic": True,
            "control_tokens": [len(tokens) for tokens in controls],
            "minimum_tokens": minimum,
        },
    )
    left = render_request(client, surface, model, ingress, {**base, **fixture.left})
    repeated = render_request(client, surface, model, ingress, {**base, **fixture.left})
    right = render_request(client, surface, model, ingress, {**base, **fixture.right})
    assert_prefix_pair(left, repeated, right, minimum)
