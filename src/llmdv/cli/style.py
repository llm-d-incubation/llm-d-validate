"""Terminal styling, decided once and honoured everywhere.

llmdv runs in two places with opposite needs: a laptop terminal, where colour and box-drawing
make a long run scannable, and a pod or CI job, where the same escape codes become literal
garbage in a captured log. So the decision is made from the stream itself rather than being
configured, and each stream is judged separately -- `llmdv verify > report.txt` can still colour
the progress on stderr while writing a clean report to the file.

`NO_COLOR` is honoured because it is the convention, and `--no-color` because a convention is
not an excuse to be unconfigurable.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass

# Set once from the CLI, read by the banner and the plugin.
_FORCE_PLAIN = False


def force_plain(enabled: bool) -> None:
    global _FORCE_PLAIN
    _FORCE_PLAIN = enabled


def _rich(stream) -> bool:
    """Whether this stream can carry colour and box drawing."""
    if _FORCE_PLAIN or os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("TERM") == "dumb":
        return False
    try:
        return bool(stream.isatty())
    except Exception:  # noqa: BLE001 -- a stream that cannot answer is not a terminal
        return False


def rich_stdout() -> bool:
    return _rich(sys.stdout)


def rich_stderr() -> bool:
    return _rich(sys.stderr)


@dataclass(frozen=True)
class Palette:
    """Escape codes, or empty strings when the stream is not a terminal.

    Returning empty strings rather than branching at every call site means the formatting code
    is written once and is correct in both modes.
    """

    reset: str = ""
    bold: str = ""
    dim: str = ""
    green: str = ""
    red: str = ""
    yellow: str = ""
    blue: str = ""

    @classmethod
    def for_stream(cls, rich: bool) -> Palette:
        if not rich:
            return cls()
        return cls(
            reset="\033[0m",
            bold="\033[1m",
            dim="\033[2m",
            green="\033[32m",
            red="\033[31m",
            yellow="\033[33m",
            blue="\033[36m",
        )

    def paint(self, text: str, colour: str) -> str:
        return f"{colour}{text}{self.reset}" if colour else text


# Box drawing degrades to ASCII that lines up identically, so a redirected run keeps its shape.
def rule_char(rich: bool) -> str:
    return "─" if rich else "-"


def separator(rich: bool) -> str:
    return " · " if rich else " | "


VERDICT_COLOURS = {"pass": "green", "fail": "red", "error": "red", "skip": "yellow"}

# Lower-case so a wall of results reads as data rather than as shouting; the colour carries the
# emphasis on a terminal, and FAIL stays upper-case because it must survive a colourless log.
VERDICT_LABELS = {"pass": "ok", "fail": "FAIL", "error": "ERR", "skip": "skip"}


def verdict(outcome: str, palette: Palette, width: int = 0) -> str:
    """A padded, coloured verdict label.

    Padding happens BEFORE colouring and is done here rather than by the caller's format
    string: `%-9s` counts the escape sequences as visible characters, so colouring first
    misaligns every column to the right of it.
    """
    label = VERDICT_LABELS.get(outcome, outcome)
    if width:
        label = label.ljust(width)
    colour = getattr(palette, VERDICT_COLOURS.get(outcome, ""), "")
    return palette.paint(label, colour)
