"""An image a client sends reaches the model as an image, on every surface that carries one.

Serving the request is not the property: a layer that drops the image, or hands the model its
base64 as text, still answers 200. Each case asks what the image shows, and the image is drawn
afresh, so the answer can only come from it.
"""

from __future__ import annotations

import pytest

from ...assertions import assert_client_error, assert_error_envelope
from ...assertions.images import assert_image_read
from ...evidence import journal
from ...surfaces.images import (
    INTERNAL_IMAGE_URLS,
    distinct_images,
    striped_image,
    two_images_question,
)
from ..conftest import budget_for, check_basic_completion
from ..streaming import check_stream_completion

pytestmark = [pytest.mark.surface_generic("image_input"), pytest.mark.needs("vision")]


def _body(surface, model, picture, *more):
    prompt = two_images_question() if more else f"This image {picture.question}"
    pngs = [item.png for item in (picture, *more)]
    return surface.image_body(model.public_id, budget_for(model, floor=256), prompt, *pngs)


def _read(client, surface, model, ingress, picture) -> None:
    exchange = client.post(surface.path, json_body=_body(surface, model, picture))
    completion = check_basic_completion(exchange, surface, model, ingress)
    assert_image_read(completion.text, picture.colours)


@pytest.mark.case_id("images.read")
def test_image_in_a_user_turn_is_read(client, surface, model, ingress):
    _read(client, surface, model, ingress, striped_image())


@pytest.mark.case_id("images.read_stream")
def test_image_is_read_in_a_streamed_request(client, surface, model, ingress):
    picture = striped_image()
    body = _body(surface, model, picture)
    body = {**surface.stream_body(model.public_id, include_usage=True), **body}
    completion = check_stream_completion(client, surface, model, ingress, body)
    assert_image_read(completion.text, picture.colours)


@pytest.mark.case_id("images.read_large")
def test_image_of_a_screenshot_s_size_is_read(client, surface, model, ingress):
    """The body is megabytes, as a screenshot's is: a layer that caps or cuts it loses the image."""
    picture = striped_image(large=True)
    journal.record("image_bytes", len(picture.png), "about 2.4 MB, more once base64-encoded")
    _read(client, surface, model, ingress, picture)


@pytest.mark.case_id("images.read_two")
def test_two_images_in_one_turn_are_read_in_order(client, surface, model, ingress):
    first, second = distinct_images(2)
    exchange = client.post(surface.path, json_body=_body(surface, model, first, second))
    completion = check_basic_completion(exchange, surface, model, ingress)
    assert_image_read(completion.text, (*first.colours, *second.colours))


@pytest.mark.case_id("images.read_distinct")
def test_the_same_question_about_another_image_gets_its_own_answer(client, surface, model, ingress):
    """Only the image differs between requests, so anything reused across them by the text alone,
    a cached prefix or a cached answer, shows as the previous image's colours."""
    first, second = distinct_images(2)
    for picture in (first, second, first):
        _read(client, surface, model, ingress, picture)


@pytest.mark.parametrize("url", INTERNAL_IMAGE_URLS, ids=INTERNAL_IMAGE_URLS.get)
@pytest.mark.disruptive("asks the fleet to fetch an address only the fleet can reach")
@pytest.mark.case_id("images.internal_url_refused")
def test_an_image_url_inside_the_fleet_is_refused(client, surface, model, ingress, url):
    """An image URL that only the serving stack can reach is refused, not fetched for the client.

    A 5xx here is the fetch failing, which means it was attempted. What this cannot see is a
    fetch that succeeds and is then refused as not an image.
    """
    body = surface.image_url_body(model.public_id, budget_for(model), "Describe the image.", url)
    exchange = client.post(surface.path, json_body=body)
    journal.record("image_url", url)
    assert_client_error(exchange, ingress, "internal_image_url")
    assert_error_envelope(exchange, surface, ingress, "internal_image_url")
