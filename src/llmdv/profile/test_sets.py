"""Named test subsets, resolved through the same case-id selection mechanism as defaults."""

from __future__ import annotations

from dataclasses import dataclass

from .errors import ProfileError
from .schema import Profile

# Deliberately explicit: adding a new suite must not silently make smoke more expensive.
# Each is an existing case with its existing gates and assertions; no duplicate smoke tests.
BUILTIN: dict[str, tuple[str, ...]] = {
    "smoke": (
        "discovery.models_lists_declared_model",
        "openai_chat.basic_non_streaming",
        "openai_chat.streaming_sse",
        "openai_completions.basic_non_streaming",
        "streaming.completions_lifecycle[True]",
        "openai_responses.basic_non_streaming",
        "openai_responses.streaming_sse",
        "anthropic_messages.basic_non_streaming",
        "anthropic_messages.streaming_sse",
    ),
}


@dataclass(frozen=True)
class TestSet:
    name: str
    source: str
    cases: tuple[str, ...]


@dataclass(frozen=True)
class Coverage:
    mode: str
    sets: tuple[TestSet, ...] = ()

    @property
    def cases(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(case for entry in self.sets for case in entry.cases))

    @property
    def label(self) -> str:
        names = ", ".join(f"{entry.name} ({entry.source})" for entry in self.sets)
        return f"{self.mode}: {names}" if names else self.mode

    def record(self) -> dict:
        return {
            "mode": self.mode,
            "sets": [
                {"name": entry.name, "source": entry.source, "cases": list(entry.cases)}
                for entry in self.sets
            ],
            "cases": list(self.cases),
        }


def available(profile: Profile) -> str:
    return ", ".join(
        f"{name} ({'built-in' if name in BUILTIN else 'profile'})"
        for name in sorted(BUILTIN.keys() | profile.test_sets.keys())
    )


def resolve(
    profile: Profile,
    names: list[str] | tuple[str, ...] = (),
    *,
    all_sets: bool = False,
    full_suite: bool = False,
) -> Coverage:
    """Resolve an explicit scope or a configured default; never fall back to a larger run."""
    if sum((bool(names), all_sets, full_suite)) > 1:
        raise ProfileError("--test-set, --all-test-sets and --full-suite are mutually exclusive")
    if full_suite:
        return Coverage("full-suite")
    hint = f"Available: {available(profile)}. Use profile show <profile> --test-sets."
    if all_sets:
        if not profile.test_sets:
            raise ProfileError("No profile-defined test sets for --all-test-sets. " + hint)
        names = sorted(profile.test_sets)
        mode = "all-test-sets"
    elif names:
        mode = "named"
    else:
        if "default" not in profile.test_sets:
            raise ProfileError(
                "No default test set is configured. Choose --test-set NAME, "
                "--all-test-sets or --full-suite. " + hint
            )
        names = ["default"]
        mode = "default"
    entries = []
    for name in dict.fromkeys(names):
        if "*" in name or "?" in name:
            raise ProfileError(
                "Test-set name patterns are not supported; use --all-test-sets. " + hint
            )
        if name in BUILTIN:
            entries.append(TestSet(name, "built-in", BUILTIN[name]))
        elif name in profile.test_sets:
            entries.append(TestSet(name, "profile", profile.test_sets[name]))
        else:
            raise ProfileError(f"Unknown test set {name!r}. " + hint)
    return Coverage(mode, tuple(entries))
