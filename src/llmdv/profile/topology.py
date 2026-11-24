"""Which ingress forwards to which: the layer graph that every cross-layer reading uses.

A profile lists its ingresses; this module says how they are stacked, in one of two ways.

- Derived (`derive`), when no ingress declares `fronts`: the edges come from each ingress's
  depth (`schema.depth`), and every ingress fronts every ingress at the next deeper depth that
  is present. The gateway fronts the router, the router fronts the engines, and two ingresses
  at one depth -- two engines, say a prefill and a decode pool -- are peers with no edge
  between them.
- Declared (`declare`), when any ingress declares `fronts`: the declared edges are the graph
  and nothing is derived. That describes arrangements depth cannot, such as a routing sidecar
  between the router and the prefill and decode engines. The loader builds it when the profile
  loads, so a bad graph is a profile error rather than a mid-run surprise.

There is one pairing rule, and both the summary's attribution ladder and the alert feed use
it: an ingress is compared only with the nearest ingresses behind it that reported. From the
ingress, step to the ones it fronts; if any of them reported, compare it with each of those and
go no further; only if none did, step on through them to the ones they front. So a gateway in
front of two routers, one of which reported, is compared with that router and not with the
engine behind it, which is that router's to be compared with. Peers are never compared, so
"prefill fails where decode passes" can no longer be printed as if decode sat behind prefill.

A run records its graph in outcome.json (`as_record`), so the alert feed reads the run without
the profile. For a run that recorded none, the feed derives one from the positions its results
recorded (see `evidence/alerts.py`).
"""

from __future__ import annotations

import heapq
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from .errors import ProfileError


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
    def declare(
        cls,
        layers: Iterable[tuple[str, Iterable[str]]],
        depths: Mapping[str, int] | None = None,
    ) -> Topology:
        """The graph a profile declares, from `(id, fronts)` in declaration order.

        Raises ProfileError naming every bad `fronts` entry: one naming no ingress, an id listed
        twice, an ingress that fronts itself, and an edge to an ingress at a shallower position
        than its own (given `depths`: an engine cannot front a router). Only once every entry is
        good is the graph itself checked, since those checks read the graph the entries
        describe and a typo would otherwise also strand the ingress it meant: a cycle (reported
        as its path) and, with more than one ingress, an ingress on no edge at all. `order` is
        topological, built by always taking the earliest-declared ingress that is ready, so
        every edge points later in it.

        An id declared twice is the loader's to refuse; here its first declaration stands.
        """
        fronts: dict[str, tuple[str, ...]] = {}
        where: dict[str, str] = {}
        for index, (name, targets) in enumerate(layers):
            if name not in fronts:
                fronts[name] = tuple(targets)
                where[name] = f"ingresses[{index}]({name})"
        names = list(fronts)
        problems: list[str] = []
        # The edges that name a known ingress other than the one declaring them, once each:
        # what the cycle and connection checks read, so one bad entry is reported once.
        edges: dict[str, tuple[str, ...]] = {}
        for name in names:
            kept: list[str] = []
            for position, target in enumerate(fronts[name]):
                at = f"{where[name]}.fronts[{position}]"
                if target not in fronts:
                    problems.append(f"{at}: no ingress {target!r}")
                elif target in fronts[name][:position]:
                    problems.append(f"{at}: lists {target!r} twice")
                elif target == name:
                    problems.append(f"{where[name]}.fronts: lists itself")
                else:
                    if depths is not None and depths[target] < depths[name]:
                        problems.append(
                            f"{at}: {target!r} is at a shallower position than {name!r}; "
                            "an ingress fronts only ingresses at its own position or deeper"
                        )
                    kept.append(target)
            edges[name] = tuple(kept)
        if problems:
            raise ProfileError(*problems)
        cycle = _cycle(names, edges)
        if cycle:
            problems.append(f"ingresses: fronts form a cycle: {' -> '.join(cycle)}")
        if len(names) > 1:
            connected = {target for targets in fronts.values() for target in targets}
            connected |= {name for name, targets in fronts.items() if targets}
            for name in names:
                if name not in connected:
                    problems.append(
                        f"{where[name]}: on no edge; it fronts nothing and nothing fronts it"
                    )
        if problems:
            raise ProfileError(*problems)
        fronts = edges
        # Kahn's algorithm, always taking the earliest-declared ingress that is ready: a heap of
        # declaration positions, so a long chain orders in one pass.
        position = {name: index for index, name in enumerate(names)}
        waiting = dict.fromkeys(names, 0)
        for targets in fronts.values():
            for target in targets:
                waiting[target] += 1
        ready = [position[name] for name in names if waiting[name] == 0]
        heapq.heapify(ready)
        order: list[str] = []
        while ready:
            name = names[heapq.heappop(ready)]
            order.append(name)
            for target in fronts[name]:
                waiting[target] -= 1
                if waiting[target] == 0:
                    heapq.heappush(ready, position[target])
        if len(order) < len(names):  # Unreachable after the cycle check; never a crash.
            raise ProfileError("ingresses: fronts cannot be ordered")
        return cls(order=tuple(order), fronts={name: fronts[name] for name in order})

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

    def behind(self, name: str) -> tuple[str, ...]:
        """Every ingress reachable from `name` through its edges, in `order`."""
        found: set[str] = set()
        stack = list(self.fronts.get(name, ()))
        while stack:
            item = stack.pop()
            if item not in found:
                found.add(item)
                stack.extend(self.fronts.get(item, ()))
        return tuple(item for item in self.order if item in found)

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


def _cycle(names: list[str], fronts: Mapping[str, tuple[str, ...]]) -> list[str]:
    """The first cycle a depth-first walk finds, as a path ending where it starts, or [].

    Walked with an explicit stack, not recursion, so no chain is too deep to check.
    """
    done: set[str] = set()
    for start in names:
        if start in done:
            continue
        path: list[str] = [start]
        on_path = {start}
        pending = [iter(fronts.get(start, ()))]
        while pending:
            target = next(pending[-1], None)
            if target is None:
                finished = path.pop()
                on_path.discard(finished)
                done.add(finished)
                pending.pop()
                continue
            if target in on_path:
                return [*path[path.index(target) :], target]
            if target in done:
                continue
            path.append(target)
            on_path.add(target)
            pending.append(iter(fronts.get(target, ())))
    return []
