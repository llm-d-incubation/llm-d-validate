"""Which ingress forwards to which: the layer graph that every cross-layer reading uses.

A profile lists its ingresses; this module says how they are stacked. The edges are derived
from each ingress's depth (`schema.depth`): every ingress fronts every ingress at the next
deeper depth that is present. LiteLLM fronts the router, the router fronts the engines, and
two ingresses at one depth -- two engines, say a prefill and a decode pool -- are peers with no
edge between them.

There is one pairing rule, and both the summary's attribution ladder and the alert feed use
it: an ingress is compared only with the nearest ingresses behind it that reported. From the
ingress, step to the ones it fronts; if any of them reported, compare it with each of those and
go no further; only if none did, step on through them to the ones they front. So a gateway in
front of two routers, one of which reported, is compared with that router and not with the
engine behind it, which is that router's to be compared with. Peers are never compared, so
"prefill fails where decode passes" can no longer be printed as if decode sat behind prefill.

A run records its graph in outcome.json (`as_record`), so the alert feed reads the run without
the profile. A run recorded before the graph existed has none, and its readers keep the depth
rule they had (see `evidence/alerts.py`).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Topology:
    """Ingress ids outermost first, and the ids each one forwards to.

    `order` is a topological order: every ingress comes before the ones it fronts. `fronts`
    has an entry for every id in `order`, empty for an ingress that fronts nothing.
    """

    order: tuple[str, ...]
    fronts: Mapping[str, tuple[str, ...]]

    @classmethod
    def derive(cls, layers: Iterable[tuple[str, int]]) -> Topology:
        """The graph implied by depth alone, from `(id, depth)` in declaration order.

        Ties keep declaration order, so `order` is exactly the stable sort by depth that
        `Profile.chain` has always returned.
        """
        ranked = sorted(layers, key=lambda layer: layer[1])
        depths = sorted({depth for _, depth in ranked})
        deeper = dict(zip(depths, depths[1:], strict=False))
        fronts = {
            name: tuple(other for other, theirs in ranked if theirs == deeper.get(mine))
            for name, mine in ranked
        }
        return cls(order=tuple(name for name, _ in ranked), fronts=fronts)

    @classmethod
    def from_record(cls, body: Any) -> Topology | None:
        """The graph a run recorded, or None when it recorded none or something unreadable.

        A readable graph can still be the wrong one for a run: see `covers`.
        """
        if not isinstance(body, dict):
            return None
        order, fronts = body.get("order"), body.get("fronts")
        if not isinstance(order, list) or not isinstance(fronts, dict):
            return None
        if not all(isinstance(name, str) for name in order) or len(set(order)) != len(order):
            return None
        # Every edge must point later in `order`, which also rules out a cycle a reader could
        # walk forever.
        edges: dict[str, tuple[str, ...]] = {}
        for index, name in enumerate(order):
            targets = fronts.get(name) or []
            if not isinstance(targets, list) or not all(t in order[index + 1 :] for t in targets):
                return None
            edges[name] = tuple(targets)
        return cls(order=tuple(order), fronts=edges)

    def as_record(self) -> dict[str, Any]:
        """The graph as outcome.json carries it."""
        return {"order": list(self.order), "fronts": {n: list(self.fronts[n]) for n in self.order}}

    def covers(self, names: Iterable[str]) -> bool:
        """Whether every one of `names` is in the graph.

        A graph that misses an ingress that reported does not describe the run, and a reader
        treats it as no graph at all rather than read the missing ingress as fronting nothing.
        """
        return set(names) <= set(self.order)

    def nearest(self, name: str, reported: Iterable[str]) -> list[str]:
        """The reported ingresses the fewest steps behind `name`.

        One step at a time: the ingresses `name` fronts, then the ones those front, and so on.
        The walk stops at the first step where any ingress reported and returns the ones that
        did; an ingress at that step that did not report is left out, and nothing behind it is
        looked at. Outermost first, in `order`. Empty when `name` is not in the graph or
        nothing behind it reported.
        """
        members = set(reported)
        seen: set[str] = set()
        level = set(self.fronts.get(name, ()))
        while level:
            found = level & members
            if found:
                return [item for item in self.order if item in found]
            seen |= level
            level = {t for item in level for t in self.fronts.get(item, ())} - seen
        return []

    def pairs(self, reported: Iterable[str]) -> list[tuple[str, str]]:
        """Every (outer, inner) the pairing rule compares among `reported`, outermost first."""
        wanted = set(reported)
        members = [item for item in self.order if item in wanted]
        return [(outer, inner) for outer in members for inner in self.nearest(outer, members)]
