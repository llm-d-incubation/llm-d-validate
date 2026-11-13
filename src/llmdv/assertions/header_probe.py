"""Backend observations prove transport dispositions, never scheduling effects."""

from __future__ import annotations

from typing import Any

from ..evidence import journal
from ..profile.schema import Disposition
from . import fail


def assert_backend_header(
    body: dict[str, Any],
    header: str,
    disposition: Disposition,
    sent: str,
    probe_id: str,
    rewrite: str | None = None,
) -> None:
    observed = body.get("llmdv_header_probe")
    if not (
        isinstance(observed, dict)
        and observed.get("instrument") == "llmdv.header_echo/1"
        and (observed.get("probe_id") == probe_id)
    ):
        fail("header_instrument", observed, "llmdv.header_echo/1 for this probe")
    allowlist = observed.get("allowlist")
    if not (isinstance(allowlist, list) and header in allowlist):
        fail("header_instrument_allowlist", allowlist, f"includes {header}")
    values = observed.get("headers")
    if not (isinstance(values, dict)):
        fail("backend_headers", values, "header mapping")
    if disposition is Disposition.STRIPPED:
        ok, expected = header not in values, "absent at backend"
    else:
        expected = sent if disposition is Disposition.FORWARDED else rewrite
        ok = expected is not None and values.get(header) == expected
    if not (ok):
        fail("backend_header_disposition", values.get(header), expected)

    journal.record(
        "backend_header_disposition",
        {"header": header, "disposition": disposition.value},
        "validated backend observation",
        ok=True,
    )
