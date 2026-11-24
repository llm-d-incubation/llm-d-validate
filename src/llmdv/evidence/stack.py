"""What the traffic said about the layers behind each ingress, kept with the run.

A response names the build that produced it: vLLM puts its commit in `system_fingerprint`,
LiteLLM stamps `x-litellm-version`, Envoy and the llm-d router sign their headers. Recording
those per run lets a comparison say "the engine changed" instead of leaving the reader to
infer it from which cases moved, and lets one run show that it straddled a rollout: two
fingerprints behind one ingress is a fleet half-way through a roll. Which headers name a build
is one table, read at every layer (`ingress/signals.py`), plus the headers each ingress
declares. The dashboard reads the same table more broadly, local-only (backend headers too).
Comparing two recorded stacks is `compare/diff.py`'s job.
"""

from __future__ import annotations

import collections
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import yaml

from ..ingress import signals, version_signals

MAX_VALUES = 5
MAX_LEN = 80

Stack = dict[str, dict[str, dict[str, int]]]


def observed_stack(
    exchanges: Iterable[dict[str, Any]], declared: dict[str, tuple[str, ...]] | None = None
) -> Stack:
    """Per ingress, each published signal's observed values with the number of responses
    carrying each. `declared` maps an ingress id to the version headers its profile declares."""
    declared = declared or {}
    per: dict[str, dict[str, collections.Counter]] = collections.defaultdict(
        lambda: collections.defaultdict(collections.Counter)
    )
    for exchange in exchanges:
        ingress = str(exchange.get("ingress") or "?")
        carried = version_signals(
            exchange.get("response_headers") or {},
            exchange.get("response_body"),
            declared.get(ingress, ()),
        )
        for name, value in carried.items():
            per[ingress][name][value[:MAX_LEN]] += 1
    return {
        ingress: {
            signal: dict(counts.most_common(MAX_VALUES))
            for signal, counts in sorted(signals.items())
        }
        for ingress, signals in sorted(per.items())
    }


def declared_version_headers(run_dir: Path) -> dict[str, tuple[str, ...]]:
    """Ingress id to the version headers the run's own profile declared (profile.used.yaml)."""
    try:
        text = (Path(run_dir) / "profile.used.yaml").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    return version_headers_in(text)


def version_headers_in(profile_text: str | None) -> dict[str, tuple[str, ...]]:
    """Ingress id to declared version headers, from a profile's YAML; empty when unreadable."""
    try:
        raw = yaml.safe_load(profile_text or "")
    except yaml.YAMLError:
        return {}
    ingresses = raw.get("ingresses") if isinstance(raw, dict) else None
    out: dict[str, tuple[str, ...]] = {}
    for ingress in ingresses if isinstance(ingresses, list) else []:
        if isinstance(ingress, dict) and ingress.get("id") and ingress.get("version_headers"):
            names = ingress["version_headers"]
            if isinstance(names, list):
                # Normalised and filtered as the loader does, so this reader and the run agree,
                # and an archived profile that declared a per-request header (before the loader
                # refused one) does not read as a rollout on every run.
                cleaned = tuple(
                    str(name).strip().lower()
                    for name in names
                    if str(name).strip() and signals.undeclarable(str(name)) is None
                )
                if cleaned:
                    out[str(ingress["id"])] = cleaned
    return out


def declared_in_both(before: str | None, after: str | None) -> dict[str, tuple[str, ...]]:
    """Per ingress, the version headers both runs' profiles declared. A run records a declared
    header only if its own profile declared it, so a header declared by one run alone is absent
    from the other's stack: comparing it would report a change that did not happen."""
    theirs, ours = version_headers_in(before), version_headers_in(after)
    out = {}
    for ingress, names in ours.items():
        shared = tuple(name for name in names if name in theirs.get(ingress, ()))
        if shared:
            out[ingress] = shared
    return out


def stack_from_run(run_dir: Path) -> Stack:
    """The stack a finished run observed: its exchanges.jsonl, read with the version headers
    its profile declared."""
    run_dir = Path(run_dir)
    return observed_stack(
        _read_jsonl(run_dir / "exchanges.jsonl"), declared_version_headers(run_dir)
    )


def _read_jsonl(path: Path):
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except ValueError:
                    continue  # a torn last line of a killed run
    except OSError:
        return
