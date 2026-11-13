"""The collector used when no cluster is reachable, or when collection is switched off.

It returns nothing rather than raising, so a run driven against a bare URL with no kubeconfig
behaves exactly like one with cluster access minus the logs.
"""

from __future__ import annotations

from pathlib import Path

from ..profile.schema import Profile
from .base import Collection


class NoopCollector:
    def __init__(self, reason: str = "log collection is off") -> None:
        self.reason = reason

    def collect(
        self,
        profile: Profile,
        ingress_ids: list[str],
        since_time: str | None,
        out_dir: Path,
    ) -> Collection:
        # Deliberately does not create out_dir: an empty logs/ directory is indistinguishable
        # from one where collection ran and found nothing.
        return Collection(reason=self.reason)

    def describe(self) -> str:
        return self.reason
