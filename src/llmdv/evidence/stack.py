"""What the traffic said about the layers behind each ingress, kept with the run.

A response names the build that produced it: vLLM puts its commit in `system_fingerprint`,
LiteLLM stamps `x-litellm-version`, Envoy and the llm-d router sign their headers. Recording
those per run lets a comparison say "the engine changed" instead of leaving the reader to
infer it from which cases moved, and lets one run show that it straddled a rollout: two
fingerprints behind one ingress is a fleet half-way through a roll. Which headers name a layer
is the ingress adapter's declaration (`IngressAdapter.version_headers`). The dashboard's stack
table is a broader, local-only view (api-base, the whole router header family) and keeps its own
list. Comparing two recorded stacks is `compare/diff.py`'s job.
"""

from __future__ import annotations

import collections
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from ..ingress import IngressAdapter, adapter_for
from ..profile.schema import IngressKind

MAX_VALUES = 5
MAX_LEN = 80

Stack = dict[str, dict[str, dict[str, int]]]


def adapter_for_kind(kind: str | None) -> IngressAdapter:
    """The adapter for an ingress kind as a run recorded it; the base one when unknown."""
    try:
        return adapter_for(IngressKind(kind)) if kind else IngressAdapter()
    except ValueError:
        return IngressAdapter()


def observed_stack(
    exchanges: Iterable[dict[str, Any]], kinds: dict[str, str] | None = None
) -> Stack:
    """Per ingress, each build signal's observed values with the number of responses carrying
    each. `kinds` maps an ingress id to its kind, so each layer's adapter says which headers
    name it; an ingress of unknown kind gets the base signals (`server`, the fingerprint)."""
    kinds = kinds or {}
    per: dict[str, dict[str, collections.Counter]] = collections.defaultdict(
        lambda: collections.defaultdict(collections.Counter)
    )
    for exchange in exchanges:
        ingress = str(exchange.get("ingress") or "?")
        headers = {
            str(k).lower(): str(v) for k, v in (exchange.get("response_headers") or {}).items()
        }
        signals = adapter_for_kind(kinds.get(ingress)).version_signals(
            headers, exchange.get("response_body")
        )
        for name, value in signals.items():
            per[ingress][name][value[:MAX_LEN]] += 1
    return {
        ingress: {
            signal: dict(counts.most_common(MAX_VALUES))
            for signal, counts in sorted(signals.items())
        }
        for ingress, signals in sorted(per.items())
    }


def ingress_kinds(records: Iterable[dict[str, Any]]) -> dict[str, str]:
    """Ingress id to kind, as results.jsonl records them."""
    kinds: dict[str, str] = {}
    for record in records:
        ingress, kind = record.get("ingress"), record.get("ingress_kind")
        if ingress and kind and ingress not in kinds:
            kinds[str(ingress)] = str(kind)
    return kinds


def stack_from_run(run_dir: Path) -> Stack:
    """The stack a finished run observed: its exchanges.jsonl, read with the ingress kinds that
    results.jsonl recorded."""
    run_dir = Path(run_dir)
    kinds = ingress_kinds(_read_jsonl(run_dir / "results.jsonl"))
    return observed_stack(_read_jsonl(run_dir / "exchanges.jsonl"), kinds)


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
