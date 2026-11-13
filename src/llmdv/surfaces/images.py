"""PNG images built in memory, and one whose content is known so an answer can be checked."""

from __future__ import annotations

import base64
import random
import struct
import zlib
from dataclasses import dataclass

# Saturated and far apart, so a vision model names each one without hedging.
COLOURS: dict[str, tuple[int, int, int]] = {
    "red": (255, 0, 0),
    "green": (0, 160, 0),
    "blue": (0, 0, 255),
    "yellow": (255, 255, 0),
    "black": (0, 0, 0),
}
STRIPE_COUNT = 4
# Named in every question, so a model that saw the image has no reason to say "lime" or "navy".
COLOUR_CHOICES = ", ".join(COLOURS)
STRIPE_WIDTH = 64
STRIPE_HEIGHT = 128
# A screenshot's size: wide enough stripes, stored without compression, come to about 2.4 MB.
LARGE_STRIPE_WIDTH = 256
LARGE_STRIPE_HEIGHT = 768
# Addresses only the serving stack itself can reach: its own loopback, and the link-local
# metadata endpoint clouds serve credentials from. No image a client names lives at either.
INTERNAL_IMAGE_URLS = {
    "http://127.0.0.1:1/llmdv.png": "loopback",
    "http://169.254.169.254/latest/meta-data/llmdv.png": "metadata",
}


def encode_png(width: int, height: int, rows: bytes, *, stored: bool = False) -> bytes:
    """An 8-bit RGB PNG from scanlines that each start with their filter byte.

    `stored` leaves the pixels uncompressed, so the file is as large as its pixels.
    """

    def chunk(kind: bytes, data: bytes) -> bytes:
        crc = struct.pack(">I", zlib.crc32(kind + data))
        return struct.pack(">I", len(data)) + kind + data + crc

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(rows, 0 if stored else -1))
        + chunk(b"IEND", b"")
    )


def png_data_url(png: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


@dataclass(frozen=True)
class Stripes:
    """Vertical stripes of solid colour, with the colour names from left to right."""

    colours: tuple[str, ...]
    png: bytes

    @property
    def question(self) -> str:
        return (
            f"shows {len(self.colours)} vertical stripes of solid colour. Name the colours "
            f"from left to right, using only these names: {COLOUR_CHOICES}. Answer with the "
            f"{len(self.colours)} colour names only, separated by commas."
        )


def striped_image(rng: random.Random | None = None, *, large: bool = False) -> Stripes:
    """Stripes drawn afresh, so the order can only come from the image itself.

    Neighbours always differ: two stripes of one colour would read as one wide stripe.
    `large` draws the same picture at a screenshot's size in bytes.
    """
    rng = rng or random.Random()
    names: list[str] = []
    while len(names) < STRIPE_COUNT:
        names.append(rng.choice([name for name in COLOURS if name not in names[-1:]]))
    width = LARGE_STRIPE_WIDTH if large else STRIPE_WIDTH
    height = LARGE_STRIPE_HEIGHT if large else STRIPE_HEIGHT
    line = b"\x00" + b"".join(bytes(COLOURS[name]) * width for name in names)
    png = encode_png(width * STRIPE_COUNT, height, line * height, stored=large)
    return Stripes(tuple(names), png)


def distinct_images(count: int, rng: random.Random | None = None) -> list[Stripes]:
    """Pictures that all differ, so an answer for one can never also be right for another."""
    rng = rng or random.Random()
    found: list[Stripes] = []
    while len(found) < count:
        picture = striped_image(rng)
        if all(picture.colours != other.colours for other in found):
            found.append(picture)
    return found


def two_images_question(count: int = STRIPE_COUNT) -> str:
    return (
        f"There are two images. Each shows {count} vertical stripes of solid colour. Name the "
        "colours of the first image from left to right, then the colours of the second image "
        f"from left to right, using only these names: {COLOUR_CHOICES}. Answer with the "
        f"{2 * count} colour names only, separated by commas."
    )
