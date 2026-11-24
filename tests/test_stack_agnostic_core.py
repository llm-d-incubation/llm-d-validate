"""The core validates any model-serving stack, not only llm-d's guides.

Guide support is data and optional conveniences on top of the core. These tests fail when code
under src/ starts depending on a guide, guide.yaml or llm-d's pod labels, or compares against a
product's name as a string literal (a text check: it cannot see a name built at run time), and
keep two profiles that use nothing guide-specific loading as standalone
profiles: a gateway reached with only a URL and a key, and a proxy in front of a router and an
engine.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from llmdv.profile import loader
from llmdv.profile.schema import IngressKind

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

# A product's name as a value the code compares against or dispatches on. Comments and prose may
# name products; a quoted literal is where a name starts deciding behaviour.
PRODUCT_LITERAL = re.compile(
    r"""(["'])(vllm|litellm|llmd_router|llm-d|sglang|trtllm|tensorrt-llm|kong|envoy|agentgateway)\1""",
    re.IGNORECASE,
)
# The loader names the product kinds a profile used to declare, only to refuse each one with
# the position that replaces it.
PRODUCT_LITERAL_ALLOWED = {("profile/loader.py", "_PRODUCT_KINDS = ")}
# A product's header name or body wording, as a value. What a response reveals lives in one table
# read at every layer; anywhere else, a literal like this ties a check to one product.
PRODUCT_SIGNAL = re.compile(
    r"""["'](x-litellm-|x-llm-d|x-envoy-|x-gateway-|x-epp|x-inference-|x-went-into"""
    r"""|inference error:)"""
)
SIGNAL_TABLE = "ingress/signals.py"


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
    "line, named",
    [
        ('if engine == "vllm":', True),
        ("kind = 'LiteLLM'", True),
        ('PRODUCT = "SGLang"', True),
        ('"""vLLM\'s stop_reason names the matched string."""', False),
        ("# vLLM answers 400 here", False),
        ('"vllm_options"', False),
    ],
)
def test_the_product_literal_check_sees_what_it_should(line: str, named: bool):
    """The check itself can fail: a quoted product name is caught in any case, prose is not."""
    assert bool(PRODUCT_LITERAL.search(line)) is named


def test_ingress_kinds_are_positions_not_products():
    """A product is the operator's to name in `id`; the core only knows where a layer sits."""
    assert {kind.value for kind in IngressKind} == {"gateway", "router", "engine"}


@pytest.mark.parametrize(
    "path",
    [p for p in SOURCES if p.suffix in {".py", ".html"}],
    ids=lambda p: str(p.relative_to(SRC)),
)
def test_no_core_module_decides_by_a_product_name(path: Path):
    relative = str(path.relative_to(SRC))
    found = [
        f"{match.group(0)} (line {number})"
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if (match := PRODUCT_LITERAL.search(line))
        and not any(
            relative == where and marker in line for where, marker in PRODUCT_LITERAL_ALLOWED
        )
    ]
    assert not found, (
        f"{path.relative_to(ROOT)} compares against a product name: {', '.join(found)}. "
        "Decide by position or by what the profile declares instead (see AGENTS.md)."
    )


@pytest.mark.parametrize(
    "path",
    [
        p
        for p in SOURCES
        if p.suffix in {".py", ".html"} and str(p.relative_to(SRC)) != SIGNAL_TABLE
    ],
    ids=lambda p: str(p.relative_to(SRC)),
)
def test_a_product_header_is_named_only_in_the_evidence_table(path: Path):
    found = [
        f"{match.group(0)} (line {number})"
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if (match := PRODUCT_SIGNAL.search(line))
    ]
    assert not found, (
        f"{path.relative_to(ROOT)} names a product's header or wording: {', '.join(found)}. "
        f"Add it to src/llmdv/{SIGNAL_TABLE} instead, so every layer reads it the same way."
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
