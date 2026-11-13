"""Error-path assertions: status, envelope and, where necessary, the rejection cause.

Comparing error wording makes a suite fail on a harmless upstream message change, so these
assert that an error is *structured* and that its status is one the profile declares for that
error class. Context overflow additionally requires a named token limit: a gateway rejecting
the body on size must not masquerade as the engine enforcing its context window.

Each records its observation into the check journal, so "this ingress correctly refused an
unknown model" is backed by the status and envelope type that were actually seen.
"""

from __future__ import annotations

import re

from ..evidence import journal
from ..http import redact
from ..http.client import Exchange
from ..profile.schema import Ingress
from ..surfaces.base import WireSurface


def tolerates(ingress: Ingress, error_class: str) -> bool:
    """Whether this layer is declared to ACCEPT this class rather than reject it.

    Expressed by declaring success statuses under `error_status`. Some layers legitimately do
    not enforce a class -- an OpenAI-compatible server commonly ignores unknown request fields
    rather than refusing them. Declaring that is better than either asserting a rejection that
    never comes, or dropping the case and testing nothing: the client-visible consequence is
    recorded, and a layer that starts enforcing it fails the declaration.
    """
    declared = ingress.expected_error_status(error_class)
    return bool(declared) and all(200 <= code < 300 for code in declared)


def assert_client_error(exchange: Exchange, ingress: Ingress, error_class: str) -> None:
    """Status is a declared one for this class, or any 4xx when none is declared."""
    # A throttle reaches error paths too. Under a quota every request answers 429, including the
    # ones meant to provoke a 400 or a 404 -- and reporting those as fleet failures is the same
    # misdirection this exists to prevent, multiplied across the suite.
    assert_not_throttled(exchange, ingress)

    assert exchange.transport_error is None, (
        f"expected an HTTP error response, got a transport failure: {exchange.transport_error}"
    )
    expected = ingress.expected_error_status(error_class)
    if expected:
        ok = exchange.status in expected
        journal.record(f"{error_class}_status", exchange.status, f"one of {list(expected)}", ok=ok)
        assert ok, (
            f"expected status in {list(expected)} for {error_class!r} at ingress "
            f"{ingress.id!r}, got {exchange.status}"
        )
        return
    else:
        ok = exchange.status is not None and 400 <= exchange.status < 500
        journal.record(f"{error_class}_status", exchange.status, "any 4xx", ok=ok)
        assert ok, (
            f"expected a 4xx for {error_class!r} at ingress {ingress.id!r}, got {exchange.status}"
        )


def assert_error_envelope(
    exchange: Exchange,
    surface: WireSurface,
    ingress: Ingress | None = None,
    error_class: str | None = None,
) -> None:
    """Assert the error body matches what this layer is declared to produce.

    Strict by default: a structured, parseable envelope with a message. Error prose is never
    compared -- the requirement is the shape, and asserting wording makes the suite fail on a
    harmless upstream message change.

    When an ingress declares an error class under `unstructured_errors`, the assertion inverts
    and requires the envelope to be ABSENT. That is deliberate rather than a way to silence a
    known gap: a layer that starts returning a parseable error fails the declaration and has to
    be re-declared, so the deviation cannot quietly become permanent, and a layer that
    regresses into returning prose fails the ordinary assertion.
    """
    envelope = surface.parse_error(exchange.response_body)
    declared_unstructured = (
        ingress is not None
        and error_class is not None
        and not ingress.produces_envelope(error_class)
    )

    if declared_unstructured:
        journal.record(
            "error_envelope",
            f"{envelope.type}/{envelope.code}" if envelope else "none",
            "declared unstructured for this layer",
            ok=envelope is None,
        )
        assert envelope is None, (
            f"{ingress.id!r} declares {error_class!r} as producing no structured envelope, but "
            f"it returned one ({envelope.type}/{envelope.code}). If the layer has been fixed, "
            f"remove {error_class!r} from its unstructured_errors."
        )
        return

    journal.record(
        "error_envelope",
        f"{envelope.type}/{envelope.code}" if envelope else None,
        f"a {surface.name} error envelope",
        ok=envelope is not None and bool(envelope.message),
    )
    assert envelope is not None, (
        f"response carried no recognisable {surface.name} error envelope, so a client SDK "
        f"cannot parse it; body was: {preview(exchange)}"
    )
    assert envelope.message, "error envelope has no message field"


def assert_no_generation(exchange: Exchange, surface: WireSurface) -> None:
    """Assert the response carries no completion.

    This is the no-silent-fallback check, and it is the half of model discovery that actually
    bites: a stack that quietly serves a different model than the one requested is a
    correctness failure no status code reveals.
    """
    served = surface.looks_like_success(exchange.response_body)
    journal.record(
        "generation_returned", served, "no generation for an unknown model", ok=not served
    )
    assert not served, (
        "response carried a generation for a model that should not exist -- the request was "
        "served by some other model rather than rejected"
    )


_CONTEXT_LIMIT = re.compile(
    r"\b(?:maximum|max)\s+context\s+(?:length|window)\s*(?:is|of|:)?\s*"
    r"([0-9][0-9,]*)\s+tokens\b",
    re.IGNORECASE,
)


def assert_context_limit_error(
    exchange: Exchange, surface: WireSurface, ingress: Ingress, max_model_len: int
) -> None:
    """Require evidence of the declared context limit, not merely a refused request.

    Match the limit phrase rather than an entire vendor message. A declared plain-text
    deviation still has to name that limit; declaring an envelope deviation cannot turn a
    proxy's unrelated rejection into context coverage. This identifies the rejection cause,
    not which physical component answered.
    """
    envelope = surface.parse_error(exchange.response_body)
    message = envelope.message if envelope is not None else None
    if not ingress.produces_envelope("context_overflow"):
        message = exchange.response_text
    limits = (
        [value.replace(",", "") for value in _CONTEXT_LIMIT.findall(message)]
        if isinstance(message, str)
        else []
    )
    rejected = exchange.status is not None and 400 <= exchange.status < 500
    matches = rejected and bool(limits) and all(value == str(max_model_len) for value in limits)
    safe_message = redact.scrub(str(message)) if message is not None else None
    journal.record(
        "context_limit_rejection",
        {"status": exchange.status, "message": safe_message, "reported_limits": limits},
        f"a 4xx naming the maximum context length of {max_model_len} tokens",
        ok=matches,
    )
    assert matches, (
        f"response did not identify the declared context limit of {max_model_len} tokens; "
        f"status={exchange.status}, message={safe_message!r}. A body-size rejection or another "
        "client error does not prove context-limit enforcement."
    )


def preview(exchange: Exchange) -> str:
    if exchange.response_body is not None:
        return repr(exchange.response_body)[:300]
    return repr(exchange.response_text)[:300]


def _skip_throttled(reason: str) -> None:
    """Skip with the throttled class attached, via the plugin that owns the skip vocabulary."""
    import pytest

    from ..plugin import mark_skip_current
    from ..profile.schema import SkipClass

    mark_skip_current(SkipClass.THROTTLED)
    pytest.skip(f"[{SkipClass.THROTTLED.value}] {reason}")


def assert_not_throttled(exchange: Exchange, ingress: Ingress) -> None:
    """Skip, rather than fail, when a layer we are rate-limited at refuses us for being over quota.

    A key with a deliberately small rate limit is how a test suite is made safe to point at a
    shared or released fleet: the request yields instead of competing. The 429 that produces is
    the limit working. Reporting it as a failure would send whoever reads the run looking for a
    fleet fault that does not exist -- and would do it at 3am, unattended, because that is when
    contention happens.

    Only where the ingress declares `rate_limited: true`. An undeclared 429 is a real finding
    about a layer we did not expect to throttle, and stays a failure.

    `Retry-After` and any `x-ratelimit-*` headers are recorded either way: a throttle with no
    indication of how long or what the quota was is a dead end for whoever investigates.
    """
    if exchange.status != 429:
        return

    headers = {name.lower(): value for name, value in (exchange.response_headers or {}).items()}
    signals = {
        name: value
        for name, value in headers.items()
        if name == "retry-after" or name.startswith("x-ratelimit-")
    }
    journal.record("throttle_signals", signals or "none returned")

    if not ingress.rate_limited:
        journal.record(
            "throttled_undeclared",
            exchange.status,
            f"ingress {ingress.id!r} does not declare rate_limited",
            ok=False,
        )
        raise AssertionError(
            f"ingress {ingress.id!r} returned 429 but the profile does not declare it rate "
            f"limited. Either a quota was applied that we do not know about, or the layer is "
            f"shedding load. Declare `rate_limited: true` if the first, investigate if the second"
        )

    journal.record("throttled", exchange.status, "declared rate_limited", ok=True)
    retry = signals.get("retry-after")
    _skip_throttled(
        f"ingress {ingress.id!r} is declared rate_limited and returned 429"
        + (f"; Retry-After: {retry}" if retry else "")
    )
