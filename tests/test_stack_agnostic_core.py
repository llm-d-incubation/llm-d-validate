"""The core validates any model-serving stack, not only llm-d's guides.

Guide support is data and optional conveniences on top of the core. These tests fail when code
under src/ starts depending on a guide, guide.yaml or llm-d's pod labels, and keep two profiles
that use nothing guide-specific loading as standalone profiles: a gateway reached with only a URL
and a key, and a proxy in front of a router and an engine.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from llmdv.profile import loader

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "llmdv"

# What a core module must not know about. Each is a fact owned by llm-d's guides, which change on
# their own schedule; the core reads such facts from a profile instead.
GUIDE_SPECIFIC = {
    "an llm-d pod label": re.compile(r"llm-d\.ai/"),
    "an llm-d guide file": re.compile(r"\bguide\.yaml\b"),
    "the guide profile directory": re.compile(r"profiles/llm-d\b"),
    "an llm-d guide name": re.compile(
        r"\b(optimized-baseline|pd-disaggregation|wide-ep|precise-prefix-cache-routing"
        r"|tiered-prefix-cache|p2p-kv-cache-sharing|predicted-latency-routing|flow-control"
        r"|multimodal-serving|coord-disaggregation|agentic-api|batch-serving"
        r"|fast-model-actuation|workload-autoscaling)\b"
    ),
}

SOURCES = sorted(p for p in SRC.rglob("*") if p.suffix in {".py", ".html", ".yaml", ".md"})


def test_there_are_sources_to_check():
    """Guards the glob: an empty list would make the check below pass vacuously."""
    assert any(p.suffix == ".py" for p in SOURCES)


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: str(p.relative_to(SRC)))
def test_no_core_module_depends_on_an_llm_d_guide(path: Path):
    text = path.read_text(encoding="utf-8", errors="replace")
    found = [
        f"{what} ({match.group(0)!r}, line {text.count(chr(10), 0, match.start()) + 1})"
        for what, pattern in GUIDE_SPECIFIC.items()
        if (match := pattern.search(text))
    ]
    assert not found, (
        f"{path.relative_to(ROOT)} refers to {', '.join(found)}. The core must validate any "
        "serving stack: read the fact from a profile instead (see AGENTS.md)."
    )


@pytest.mark.parametrize(
    "name",
    [
        "example-gateway-only.yaml",  # one gateway: a URL and a key are all you have
        "example-three-ingress.yaml",  # a proxy in front of a router and an engine
    ],
)
def test_a_stack_without_a_guide_loads_as_a_standalone_profile(name: str):
    """No extends and no guide parts: the profile alone describes the stack."""
    path = ROOT / "profiles" / "examples" / name
    raw = path.read_text(encoding="utf-8")
    assert "extends:" not in raw, f"{name} must stay a standalone profile"
    profile = loader.load(path)
    assert profile.ingresses, f"{name} declares no ingress"
