"""results.jsonl -- the structured result relation.

A compatibility matrix is a pivot of this file, so the shape matters more than the formatting.
Three properties are deliberate:

`schema` is versioned, so matrices from different dates, fleets and llmdv versions can be
diffed rather than eyeballed.

`skip_class` keeps six reasons apart. A matrix that renders "the fleet does not expose this"
and "llmdv cannot test it yet" as one grey box reads as coverage, which is worse than admitting
the gap.

`served_by` is recorded from the first pass although nothing reads it until `llmdv matrix`
exists, because attribution without it produces confident wrong answers -- see
`llmdv/ingress/base.py` for the per-replica configuration asymmetry that motivates it.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .. import __version__
from ..http import redact

SCHEMA = "llmdv.results/1"


def new_run_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")
    return f"{stamp}-{uuid.uuid4().hex[:4]}"


@dataclass
class ResultRecord:
    run_id: str
    nodeid: str
    outcome: str  # pass | fail | skip | error
    profile: str
    schema: str = SCHEMA
    ts: str = ""
    llmdv_version: str = __version__
    profile_digest: str | None = None
    case_id: str | None = None
    # The parametrisation other than the ingress, e.g. which header a disposition case checked.
    # Without it, six header cases collapse into one indistinguishable matrix row.
    variant: str | None = None
    clients: list[str] = field(default_factory=list)
    ingress: str | None = None
    ingress_kind: str | None = None
    surface: str | None = None
    skip_class: str | None = None
    reason: str | None = None
    duration_s: float = 0.0
    http_status: int | None = None
    served_by: dict[str, Any] = field(default_factory=dict)
    # What the assertions actually looked at, and what they saw. This is what makes a "pass"
    # auditable rather than a bare claim that nothing raised.
    checks: list[dict[str, Any]] = field(default_factory=list)
    evidence: str | None = None
    # What the profile's `known_failures` say about this result, when they say anything:
    # the issue, the fix and a status (`known` or `fix_expected`). The outcome above
    # is untouched by it. See profile/known.py.
    known_failure: dict[str, Any] | None = None
    # This describes stored evidence, not the verdict, which was computed from the full stream.
    partial_evidence: bool = False

    def __post_init__(self) -> None:
        if not self.ts:
            self.ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds")

    def to_json(self) -> str:
        # A reason or check can quote what the fleet sent back, credentials included.
        return json.dumps(redact.redact_value(asdict(self)), sort_keys=False)


class ResultsWriter:
    """Append-only JSONL writer.

    Appends and flushes per record rather than buffering: a suite interrupted halfway should
    still leave the results it had already produced.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a", encoding="utf-8", buffering=1)
        self.records: list[ResultRecord] = []

    def append(self, record: ResultRecord) -> None:
        self.records.append(record)
        self._handle.write(record.to_json() + "\n")

    def close(self) -> None:
        self._handle.close()

    # ------------------------------------------------------------- summarising

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for record in self.records:
            out[record.outcome] = out.get(record.outcome, 0) + 1
        return out

    def matrix(self) -> dict[str, dict[str, str]]:
        """case_id[variant] -> ingress -> outcome. The precursor to `llmdv matrix`.

        The variant is part of the row key -- the surface for a surface-generic case, the header
        for a disposition case -- in the same `case_id[variant]` form the run summary uses.
        Keyed by case_id alone, a chat failure and a completions pass of one case would share a
        cell, and whichever came last would win.
        """
        grid: dict[str, dict[str, str]] = {}
        for record in self.records:
            if not record.case_id or not record.ingress:
                continue
            row = f"{record.case_id}[{record.variant}]" if record.variant else record.case_id
            cell = record.outcome
            if record.outcome == "skip" and record.skip_class:
                cell = f"skip:{record.skip_class}"
            grid.setdefault(row, {})[record.ingress] = cell
        return grid
