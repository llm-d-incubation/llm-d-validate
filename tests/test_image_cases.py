"""The image cases against a fake that reads a picture, and one that never receives it."""

from __future__ import annotations

import random

import pytest
import yaml

from llmdv.assertions.images import assert_image_read, colours_named
from llmdv.evidence import journal
from llmdv.profile.loader import ProfileError, load
from llmdv.surfaces import get as get_surface
from llmdv.surfaces.images import (
    COLOURS,
    INTERNAL_IMAGE_URLS,
    STRIPE_COUNT,
    distinct_images,
    png_data_url,
    striped_image,
)
from tests import tool_engine
from tests.fakes import FakeFleet
from tests.test_suites_against_fake import run_suites, write_profile

ALL = ("chat_completions", "messages", "responses")
# The fake refuses images, as a text-only model does, unless told it reads them.
MULTIMODAL = {"multimodal": True}


READ = "test_image_in_a_user_turn"


def run_all(tmp_path, select, defect=None, declared=True, **capabilities):
    """Run the selected image cases on every surface; every record, and the requests sent."""
    with FakeFleet(tool_engine=True, tool_engine_defect=defect, transport=MULTIMODAL) as fleet:
        path = write_profile(tmp_path, fleet)
        raw = yaml.safe_load(path.read_text())
        raw["ingresses"][0]["surfaces"] = list(ALL)
        raw["capabilities"].update(vision=declared, **capabilities)
        path.write_text(yaml.safe_dump(raw))
        code, records = run_suites(path, tmp_path / "run", "-m", "images", "-k", select)
        sent = len(fleet.seen)
    return code, [r for r in records if r.get("surface") in ALL], sent


def run(tmp_path, defect=None, declared=True):
    code, records, sent = run_all(tmp_path, READ, defect, declared)
    return code, {r["surface"]: r for r in records}, sent


def failed_checks(record):
    return [c["name"] for c in record["checks"] if c.get("ok") is False]


def test_an_image_the_model_reads_passes_on_every_surface(tmp_path):
    code, rows, sent = run(tmp_path)
    assert code == 0, rows
    assert {surface: row["outcome"] for surface, row in rows.items()} == dict.fromkeys(ALL, "pass")
    assert sent == len(ALL)
    for row in rows.values():
        assert any(c["name"] == "image_content_read" and c["ok"] for c in row["checks"])


def test_an_image_that_never_reaches_the_model_fails(tmp_path):
    code, rows, _ = run(tmp_path, "drops_user_image")
    assert code == 1
    for surface in ALL:
        assert rows[surface]["outcome"] == "fail", rows[surface]
        failed = [c["name"] for c in rows[surface]["checks"] if c.get("ok") is False]
        assert failed == ["image_content_read"]


def test_undeclared_vision_sends_nothing(tmp_path):
    code, rows, sent = run(tmp_path, declared=False)
    assert code == 0
    assert {row["skip_class"] for row in rows.values()} == {"undeclared"}
    assert sent == 0


def test_vision_is_declared_per_surface(tmp_path):
    with FakeFleet(tool_engine=True, transport=MULTIMODAL) as fleet:
        path = write_profile(tmp_path, fleet)
        raw = yaml.safe_load(path.read_text())
        raw["ingresses"][0]["surfaces"] = list(ALL)
        raw["capabilities"]["vision"] = ["messages"]
        path.write_text(yaml.safe_dump(raw))
        _, records = run_suites(path, tmp_path / "run", "-k", READ)
    outcome = {r["surface"]: r["outcome"] for r in records if r.get("surface") in ALL}
    assert outcome == {"chat_completions": "skip", "messages": "pass", "responses": "skip"}


def test_vision_rejects_an_unknown_surface(tmp_path):
    with FakeFleet() as fleet:
        path = write_profile(tmp_path, fleet)
    raw = yaml.safe_load(path.read_text())
    raw["capabilities"]["vision"] = ["telepathy"]
    path.write_text(yaml.safe_dump(raw))
    with pytest.raises(ProfileError, match="vision"):
        load(path)


def test_the_striped_image_is_a_png_the_engine_reads_back():
    for seed in range(50):
        picture = striped_image(random.Random(seed))
        assert len(picture.colours) == STRIPE_COUNT
        assert all(a != b for a, b in zip(picture.colours, picture.colours[1:], strict=False))
        encoded = png_data_url(picture.png).partition(",")[2]
        assert tool_engine._stripes(encoded) == ", ".join(picture.colours)


def test_the_engine_palette_matches_the_colours_the_case_draws():
    assert {name: rgb for rgb, name in tool_engine.PALETTE.items()} == COLOURS


@pytest.mark.parametrize("surface", ALL)
def test_each_surface_puts_the_image_before_the_question(surface):
    picture = striped_image()
    body = get_surface(surface).image_body("m", 64, "What is this?", picture.png)
    turn = (body.get("messages") or body["input"])[0]
    kinds = [part["type"] for part in turn["content"]]
    assert kinds[0] in ("image", "image_url", "input_image")
    assert kinds[1] in ("text", "input_text")
    assert tool_engine._user_images(tool_engine.PATHS[get_surface(surface).path], body)


@pytest.mark.parametrize(
    "answer,ok",
    [
        ("red, blue, green, black", True),
        ("The stripes are Red, Blue, Green and Black.", True),
        ("Left to right: red, blue, green, black. So: red, blue, green, black", True),
        ("blue, red, green, black", False),
        ("red, blue, green", False),
        ("I cannot see any image.", False),
        ("", False),
    ],
)
def test_an_answer_must_name_the_colours_in_order(answer, ok):
    active = journal.activate("images-unit")
    try:
        if ok:
            assert_image_read(answer, ("red", "blue", "green", "black"))
        else:
            with pytest.raises(AssertionError):
                assert_image_read(answer, ("red", "blue", "green", "black"))
    finally:
        journal.deactivate()
    assert [c.ok for c in active.checks if c.name == "image_content_read"] == [ok]


def test_colour_names_inside_other_words_are_not_counted():
    assert colours_named("The reddish, blackened Greenland bluebird") == []


# ------------------------------------------------- the cases beyond one image in one request


@pytest.mark.parametrize(
    "select,defect,check",
    [
        ("test_image_is_read_in_a_streamed_request", None, None),
        ("test_image_is_read_in_a_streamed_request", "drops_user_image", "image_content_read"),
        ("test_image_of_a_screenshot_s_size_is_read", None, None),
        ("test_image_of_a_screenshot_s_size_is_read", "large_image_413", "http_status"),
        ("test_two_images_in_one_turn_are_read_in_order", None, None),
        ("test_two_images_in_one_turn", "drops_second_image", "image_content_read"),
        ("test_the_same_question_about_another_image", None, None),
        ("test_the_same_question_about_another_image", "text_keyed_answer", "image_content_read"),
        ("test_an_image_url_inside_the_fleet_is_refused", None, None),
        (
            "test_an_image_url_inside_the_fleet_is_refused",
            "fetches_image_urls",
            "internal_image_url_status",
        ),
    ],
)
def test_each_image_case_passes_and_catches_its_defect(tmp_path, select, defect, check):
    code, records, _ = run_all(tmp_path, select, defect)
    assert records
    assert {r["surface"] for r in records} == set(ALL)
    assert code == (1 if defect else 0), records
    for record in records:
        assert record["outcome"] == ("fail" if defect else "pass"), record
        if defect:
            assert check in failed_checks(record), record["checks"]


def test_a_large_image_is_megabytes_and_still_read_back():
    picture = striped_image(random.Random(3), large=True)
    assert 2_000_000 < len(picture.png) < 5_000_000
    encoded = png_data_url(picture.png).partition(",")[2]
    assert tool_engine._stripes(encoded) == ", ".join(picture.colours)


def test_distinct_images_never_repeat():
    pictures = distinct_images(40, random.Random(5))
    assert len({picture.colours for picture in pictures}) == 40


@pytest.mark.parametrize("surface", ALL)
def test_each_surface_keeps_two_images_in_order(surface):
    first, second = distinct_images(2)
    body = get_surface(surface).image_body("m", 64, "Which?", first.png, second.png)
    images = tool_engine._user_images(tool_engine.PATHS[get_surface(surface).path], body)
    assert [tool_engine._stripes(image) for image in images] == [
        ", ".join(first.colours),
        ", ".join(second.colours),
    ]


@pytest.mark.parametrize("surface", ALL)
def test_each_surface_names_a_url_image_without_data(surface):
    url = next(iter(INTERNAL_IMAGE_URLS))
    body = get_surface(surface).image_url_body("m", 64, "Which?", url)
    api = tool_engine.PATHS[get_surface(surface).path]
    assert [tool_engine._image_url(part) for part in tool_engine._user_parts(api, body)] == [url]
    assert tool_engine._user_images(api, body) == []


def test_a_fleet_declared_to_answer_internal_urls_otherwise_is_held_to_that(tmp_path):
    with FakeFleet(
        tool_engine=True, tool_engine_defect="fetches_image_urls", transport=MULTIMODAL
    ) as fleet:
        path = write_profile(tmp_path, fleet)
        raw = yaml.safe_load(path.read_text())
        raw["ingresses"][0]["surfaces"] = ["messages"]
        raw["ingresses"][0]["error_status"]["internal_image_url"] = [500]
        raw["capabilities"]["vision"] = True
        path.write_text(yaml.safe_dump(raw))
        code, records = run_suites(path, tmp_path / "run", "-k", "test_an_image_url_inside")
    assert code == 0, [(r["outcome"], r.get("message"), failed_checks(r)) for r in records]


# ------------------------------------------------------------------------- token counting


@pytest.mark.parametrize(
    "select,defect,check",
    [
        ("test_an_image_is_counted_as", None, None),
        ("test_an_image_is_counted_as", "count_ignores_image", "count_tokens_matches_usage"),
        ("test_an_image_is_counted_as", "count_image_as_text", "image_counted"),
        ("test_an_image_counted_first_is_still_served", None, None),
        ("test_an_image_counted_first_is_still_served", "count_poisons_image", "http_status"),
    ],
)
def test_count_cases_pass_and_catch_their_defects(tmp_path, select, defect, check):
    code, records, _ = run_all(tmp_path, select, defect, count_tokens=True)
    assert [r["surface"] for r in records] == ["messages"]
    assert code == (1 if defect else 0), records
    assert records[0]["outcome"] == ("fail" if defect else "pass"), records[0]
    if defect:
        assert check in failed_checks(records[0])


def test_count_cases_need_token_counting_declared(tmp_path):
    code, records, sent = run_all(tmp_path, "test_an_image_is_counted_as", count_tokens=False)
    assert code == 0 and sent == 0
    assert {r["skip_class"] for r in records} == {"undeclared"}
