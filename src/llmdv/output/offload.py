"""Synchronous, best-effort storage for evidence too large for the run directory."""

from __future__ import annotations

import hashlib

from ..http.redact import scrub
from ..log.setup import get_logger
from .remote import Patience, sink_for

log = get_logger("sink.offload")

OFFLOAD = Patience(attempts=3, connect=5, read=30)


def run_prefix(profile_name: str, run_id: str) -> str:
    return f"{profile_name}/{run_id}"


def reference(data: bytes, kept_bytes: int, offload: dict) -> dict:
    return {
        "schema": "llmdv.artifact/1",
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "kept_bytes": kept_bytes,
        "truncated": len(data) != kept_bytes,
        "offload": offload,
    }


def whole(ref: dict | None) -> bool:
    return bool(
        ref and (not ref.get("truncated") or ref.get("offload", {}).get("status") == "stored")
    )


class Offloader:
    def __init__(self, uri: str | None, run_name: str) -> None:
        self.uri = uri
        self.run_name = run_name
        self._sink = None
        self._failure: str | None = None

    def store(self, relpath: str, data: bytes) -> dict:
        if not self.uri:
            return {"status": "not_configured", "error": "pass --artifact-sink to keep it whole"}
        if self._failure:
            return {"status": "skipped", "error": f"an earlier upload failed: {self._failure}"}
        try:
            if self._sink is None:
                self._sink = sink_for(self.uri, patience=OFFLOAD)
            uri = self._sink.put_bytes(data, self.run_name, "offloaded", relpath)
            return {"status": "stored", "uri": uri}
        except Exception as exc:  # noqa: BLE001 -- evidence fallback must not fail the run
            self._failure = scrub(str(exc))
            log.warning("could not offload %s: %s", relpath, self._failure)
            return {"status": "failed", "error": self._failure}
