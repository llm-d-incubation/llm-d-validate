"""The README's coverage graphic stays in step with the suites and with its generator."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

from llmdv.plugin import suite_markers

ROOT = Path(__file__).resolve().parents[1]
GENERATOR = ROOT / "docs" / "images" / "make_coverage_svg.py"
SVG = ROOT / "docs" / "images" / "llmdv-coverage.svg"
REGENERATE = "python docs/images/make_coverage_svg.py"


def _generator() -> ModuleType:
    spec = importlib.util.spec_from_file_location("make_coverage_svg", GENERATOR)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_graphic_names_every_suite_once() -> None:
    shown = _generator().markers()
    suites = set(suite_markers())
    duplicated = sorted({m for m in shown if shown.count(m) > 1})
    missing = sorted(suites - set(shown))
    extra = sorted(set(shown) - suites)
    assert not duplicated, f"coverage graphic lists these suites twice: {duplicated}"
    assert not missing, (
        f"suites missing from the coverage graphic: {missing}. Add each to a group in "
        f"GROUPS in {GENERATOR.relative_to(ROOT)}, then run `{REGENERATE}`"
    )
    assert not extra, (
        f"coverage graphic names markers that are not suites: {extra}. Remove them from "
        f"GROUPS in {GENERATOR.relative_to(ROOT)}, then run `{REGENERATE}`"
    )


def test_committed_graphic_is_what_the_generator_renders() -> None:
    assert SVG.read_text(encoding="utf-8") == _generator().render(), (
        f"{SVG.relative_to(ROOT)} differs from what its generator renders: edit the generator, "
        f"not the SVG, and run `{REGENERATE}`"
    )
