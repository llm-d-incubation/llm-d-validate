"""Whether an answer shows that the model saw an image, not only that the request was served."""

from __future__ import annotations

import re
from collections.abc import Sequence

from ..evidence import journal
from ..surfaces.images import COLOURS
from . import fail

_COLOUR = re.compile(r"\b(" + "|".join(COLOURS) + r")\b")


def colours_named(text: str) -> list[str]:
    """Every known colour name in the text, in the order it is written."""
    return _COLOUR.findall(text.lower())


def assert_image_read(text: str, colours: Sequence[str]) -> None:
    """The answer names the image's colours in order.

    The order is drawn afresh for each image, so it can only come from the image: a layer that
    drops the image, or hands the model its base64 text, leaves nothing to read it from.
    Only the five names in COLOURS count; every question lists them, so a synonym such as
    "lime" means the model ignored the instruction, not that it missed the image.
    """
    named = colours_named(text)
    wanted = list(colours)
    runs = (named[start : start + len(wanted)] for start in range(len(named)))
    expected = f"the colours {', '.join(wanted)} in that order"
    if wanted not in runs:
        fail("image_content_read", {"named": named[:16], "answer": text[:300]}, expected)
    journal.record("image_content_read", named[:16], expected, ok=True)
