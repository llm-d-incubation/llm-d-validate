"""Prefix reuse when the prompt starts with an image: the image is part of the prefix."""

import pytest

from ...evidence import journal
from ...surfaces.images import distinct_images
from .helpers import cache_config, check_cold, check_replicas, check_warm, observe, probe_prompt


@pytest.mark.surface_generic("cache_usage", "image_input")
@pytest.mark.needs("cache_reuse", "vision")
@pytest.mark.case_id("cache.image_reuse")
def test_cache_reuse_follows_the_image(client, surface, model, ingress):
    """The same image and text twice is a warm repeat; another image before that text is not.

    A cache keyed on the text alone would hand the second image the first one's prefix.
    """
    config = cache_config(ingress, surface)
    prompt = probe_prompt(config)
    one, other = distinct_images(2)
    bodies = {
        "first": surface.image_body(model.public_id, 16, prompt, one.png),
        "other_image": surface.image_body(model.public_id, 16, prompt, other.png),
    }
    first = observe(client, surface, model, ingress, bodies["first"], "first")
    repeat = observe(client, surface, model, ingress, bodies["first"], "repeat")
    changed = observe(client, surface, model, ingress, bodies["other_image"], "other_image")
    check_replicas([first, repeat, changed], config)
    check_cold(first, config)
    check_warm(first, repeat, config)
    # Not check_cold: a template preamble before the image can fill a whole block and be
    # reused legitimately. Reading less than the same image's repeat is what proves the point.
    ok = changed.read < repeat.read
    journal.record(
        "cache_follows_image",
        {"same_image_read": repeat.read, "other_image_read": changed.read},
        "another image reads less from cache than the same image",
        ok=ok,
    )
    assert ok, "a different image reused as much cached prefix as the same image"
