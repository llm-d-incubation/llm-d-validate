"""HTTP-level assertions: served or refused, never a server error, and quickly.

Many transport behaviors have two acceptable answers. A layer may decode a gzip body or refuse
it with 415; it may accept a charset parameter or a missing Content-Type, or refuse them. What
no layer may do is answer a client's ordinary mistake with a 5xx, hang, or drop the connection,
because SDKs retry 5xx and users read them as outages. Where a profile pins the status for an
error class, the declaration decides instead.
"""

from __future__ import annotations

from ..evidence import journal
from ..http.client import Exchange
from ..profile.schema import Ingress
from .errors import assert_error_envelope, assert_not_throttled

# A refusal is a cheap decision. Anything slower suggests the request reached work it should not.
FAST_S = 10.0

# Statuses both pinned SDKs retry by default (openai and anthropic `_should_retry`).
SDK_RETRY_STATUSES = frozenset({408, 409, 429})


def status_class(status: int | None) -> str:
    return "none" if status is None else f"{status // 100}xx"


def assert_answered(exchange: Exchange, what: str) -> None:
    """An HTTP answer arrived: no transport error and no 5xx."""
    ok = exchange.transport_error is None and exchange.status is not None
    journal.record(
        f"{what}_answered", exchange.transport_error or exchange.status, "an HTTP answer", ok=ok
    )
    assert ok, f"{what}: no HTTP answer: {exchange.transport_error}"
    ok = exchange.status < 500
    journal.record(f"{what}_status", exchange.status, "not 5xx", ok=ok)
    assert ok, f"{what}: answered {exchange.status}; a client mistake must never be a server error"


def assert_fast(exchange: Exchange, what: str, limit: float = FAST_S) -> None:
    ok = exchange.elapsed_s <= limit
    journal.record(f"{what}_elapsed_s", round(exchange.elapsed_s, 3), f"at most {limit}s", ok=ok)
    assert ok, f"{what}: took {exchange.elapsed_s:.1f}s, above {limit}s"


def served_or_refused(
    exchange: Exchange, surface, ingress: Ingress, error_class: str, what: str
) -> bool:
    """Check a request that may be served or refused. Returns True when it was served.

    With statuses declared for `error_class`, the status must be one of them. Otherwise a 2xx
    or a 4xx passes and anything else fails. A refusal must carry the surface's error envelope
    unless the ingress declares the class unstructured, and must be fast.
    """
    assert_not_throttled(exchange, ingress)
    assert_answered(exchange, what)
    declared = ingress.expected_error_status(error_class)
    status = exchange.status
    if declared:
        ok = status in declared
        expected = f"one of {list(declared)}"
    else:
        ok = 200 <= status < 300 or 400 <= status < 500
        expected = "2xx or 4xx"
    journal.record(f"{what}_outcome", status, expected, ok=ok)
    assert ok, f"{what}: expected {expected} at {ingress.id!r}, got {status}"
    if 200 <= status < 300:
        return True
    if status >= 400:
        assert_error_envelope(exchange, surface, ingress, error_class)
        assert_fast(exchange, what)
    return False
