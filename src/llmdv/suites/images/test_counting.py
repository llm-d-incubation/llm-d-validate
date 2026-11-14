"""Token counting takes the image a generation takes, and counts it as an image."""

from __future__ import annotations

import pytest

from ...assertions.context import assert_count_matches_usage, prompt_size
from ...assertions.images import assert_image_read
from ...assertions.messages import assert_messages_token_count
from ...evidence import journal
from ...surfaces.images import striped_image
from ..conftest import budget_for, check_basic_completion, check_json_response

pytestmark = [pytest.mark.surface("messages"), pytest.mark.needs("count_tokens", "vision")]

# An image is a few hundred tokens of vision input. A count that reads its base64 as text is
# hundreds of thousands for a picture this size.
IMAGE_COUNT_CEILING = 5000


def _count(client, surface, ingress, **body) -> int:
    exchange = client.post(surface.count_tokens_path, json_body=body)
    return assert_messages_token_count(check_json_response(exchange, ingress))


def _generation(surface, model, picture):
    prompt = f"This image {picture.question}"
    body = surface.image_body(model.public_id, budget_for(model, floor=256), prompt, picture.png)
    return prompt, body


@pytest.mark.case_id("images.counted")
def test_an_image_is_counted_as_the_generation_counts_it(client, surface, model, ingress):
    picture = striped_image(large=True)
    prompt, generation = _generation(surface, model, picture)
    # Generated first, so the count is compared with a prompt the fleet has already served.
    completion = check_basic_completion(
        client.post(surface.path, json_body=generation), surface, model, ingress
    )
    alone = _count(client, surface, ingress, **surface.count_tokens_body(model.public_id, prompt))
    counted = surface.count_tokens_body(model.public_id, messages=generation["messages"])
    with_image = _count(client, surface, ingress, **counted)
    ok = alone < with_image < IMAGE_COUNT_CEILING
    journal.record(
        "image_counted",
        {"question_alone": alone, "with_image": with_image},
        f"more than the question alone and below {IMAGE_COUNT_CEILING}",
        ok=ok,
    )
    assert ok, f"count_tokens did not count the image as an image ({alone} -> {with_image})"
    # The whole prompt: an engine that caches reports the cached part of it separately.
    assert_count_matches_usage(with_image, prompt_size(surface, completion))


@pytest.mark.case_id("images.generate_after_count")
def test_an_image_counted_first_is_still_served(client, surface, model, ingress):
    """A client counts a prompt and then sends it. Counting an image the fleet has not seen
    must not leave the generation that follows unable to find it."""
    for picture in (striped_image(), striped_image(large=True)):
        _, generation = _generation(surface, model, picture)
        counted = surface.count_tokens_body(model.public_id, messages=generation["messages"])
        _count(client, surface, ingress, **counted)
        completion = check_basic_completion(
            client.post(surface.path, json_body=generation), surface, model, ingress
        )
        assert_image_read(completion.text, picture.colours)
