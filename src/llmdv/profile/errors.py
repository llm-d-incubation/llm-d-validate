"""The two ways a profile is reported wrong.

A `ProfileError` means the file cannot be turned into a `Profile` at all, so nothing runs. A
`Diagnostic` is one finding of `validate` on a profile that did load: an error blocks a run, a
warning does not. Every module of the profile package raises or returns these and nothing else,
so a caller catches one exception type.
"""

from __future__ import annotations

from dataclasses import dataclass


class ProfileError(Exception):
    """Raised for a profile that cannot be turned into an object at all."""


@dataclass(frozen=True)
class Diagnostic:
    level: str  # "error" | "warning"
    path: str
    message: str

    def __str__(self) -> str:
        return f"{self.level.upper():<7} {self.path}: {self.message}"
