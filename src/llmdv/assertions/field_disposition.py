"""Judge a request field against the disposition an ingress declares for it.

`accepted` and `ignored` both require an ordinary generation; the difference is what the profile
documents to a reader. `rejected` requires a clean 4xx with the surface's error envelope. A 5xx
fails under every declaration: a client SDK retries it, and the retry can never succeed.
"""

from __future__ import annotations

from ..evidence import journal
from ..http.client import Exchange
from ..profile.schema import Ingress
from ..surfaces.base import WireSurface
from . import fail
from .errors import assert_error_envelope, assert_not_throttled


def assert_field_status(
    exchange: Exchange, surface: WireSurface, ingress: Ingress, name: str, disposition: str
) -> bool:
    """Check the status the declaration allows; True when the request generated."""
    assert_not_throttled(exchange, ingress)
    journal.record("field_declared", {"field": name, "disposition": disposition})
    if exchange.transport_error is not None:
        fail("field_status", exchange.transport_error, "an HTTP response")
    status = exchange.status
    if status is None or status >= 500:
        fail("field_status", status, f"200 or a clean 4xx for {name}")
    if disposition == "rejected":
        if not 400 <= status < 500:
            fail("field_status", status, f"a 4xx refusing {name}, as declared")
        journal.record("field_status", status, f"a 4xx refusing {name}", ok=True)
        assert_error_envelope(exchange, surface, ingress, "unsupported_option")
        return False
    if status != 200:
        fail("field_status", status, f"200 with {name} {disposition}, as declared")
    journal.record("field_status", status, f"200 with {name} {disposition}", ok=True)
    return True
