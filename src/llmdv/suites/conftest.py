"""Shared helpers for the functional suites.

Fixtures come from `llmdv.plugin`, including the ingress-specific `model`.
The plugin is loaded through a `pytest11` entry point when llmdv is installed and with
`-p llmdv.plugin` when running from a source checkout.
"""

from __future__ import annotations

import random
import uuid
from contextlib import contextmanager
from typing import Any

import httpx
import pytest

from ..assertions import (
    assert_answer_only,
    assert_has_answer,
    assert_has_output,
    assert_healthy_finish,
    assert_not_throttled,
    assert_usage_present_and_consistent,
)
from ..assertions.errors import _skip_throttled
from ..assertions.tool_calls import assert_no_call_markup
from ..evidence import journal
from ..http.client import Exchange
from ..profile.schema import AuthType, Ingress, Model, ProxyMode
from ..surfaces import Completion, Surface
from ..surfaces.base import DEFAULT_ANSWER


def nonexistent_model_name() -> str:
    """A model id that cannot exist, fresh per call.

    Random per call so a previous run cannot have warmed a cache or an alias table with it,
    which would turn the no-fallback assertion into a test of leftover state.
    """
    return f"llmdv-nonexistent-{uuid.uuid4().hex[:12]}"


_DOCUMENT_WORDS = (
    "river stone maple copper lantern harbor meadow saddle timber falcon glacier orchard pepper "
    "violin anchor basket candle desert ember fossil garden hammer island jacket kettle ladder "
    "marble needle oyster pillow rocket silver tunnel velvet walnut bridge cactus dolphin engine "
    "feather garnet helmet jungle lemon magnet nectar olive quartz radar salmon tiger umbrella"
).split()


def nonce_document(words: int) -> str:
    """A document of `words` common words, headed by a nonce no earlier request has carried.

    Every reuse probe starts from one, so nothing an earlier run cached can be read back: the
    nonce leads, and the words after it are drawn from a list seeded by it. Common words keep
    the token count close to the word count.
    """
    nonce = uuid.uuid4().hex
    rng = random.Random(nonce)
    drawn = [rng.choice(_DOCUMENT_WORDS) for _ in range(words)]
    paragraphs = (" ".join(drawn[start : start + 60]) for start in range(0, words, 60))
    return f"Reference {nonce}.\n\n" + "\n\n".join(paragraphs)


def budget_for(model: Model, floor: int = 64) -> int:
    """A `max_tokens` large enough that a thinking model still produces something.

    A reasoning-by-default fleet can spend the entire budget inside a `<think>` block, so a
    tight budget yields finish_reason="length" with empty content -- a healthy response that a
    naive assertion reads as a failure. The floor is raised when reasoning is on by default.
    """
    return floor * 4 if model.reasoning.default_on else floor


def check_answer_budget(client, surface: Surface, model: Model, ingress: Ingress) -> None:
    """One simple prompt at the declared budget, answered alone, with no mode override or retry."""
    budget = model.answer_max_tokens
    if budget is None:
        pytest.skip(f"[undeclared] ingress {ingress.id!r} declares no model.answer_max_tokens")
    journal.record("answer_max_tokens", budget)
    body = surface.minimal_body(model.public_id, max_tokens=budget)
    exchange = client.post(surface.path, json_body=body)
    completion = check_basic_completion(exchange, surface, model, ingress)
    assert_has_answer(completion, budget)
    assert_answer_only(completion, model, DEFAULT_ANSWER)


def check_basic_completion(
    exchange: Exchange,
    surface: Surface,
    model: Model,
    ingress: Ingress,
    *,
    quotes_markup: bool = False,
) -> Completion:
    """Record and check the shared contract for a non-streaming generation."""
    body = check_json_response(exchange, ingress)
    completion = surface.parse_completion(body)
    check_completion(completion, surface, model, quotes_markup=quotes_markup)
    return completion


def check_json_response(exchange: Exchange, ingress: Ingress) -> dict[str, Any]:
    """Check transport, HTTP status and JSON before surface-specific assertions.

    Throttling is handled first, and here rather than per case: this is the one place every
    happy-path case asserts a 200, so it is the only place a 429 has to be understood.
    """
    assert_not_throttled(exchange, ingress)
    journal.record("http_status", exchange.status, "200", ok=exchange.status == 200)
    assert exchange.transport_error is None, f"transport failure: {exchange.transport_error}"
    assert exchange.status == 200, (
        f"expected 200 from {ingress.id!r}, got {exchange.status}: "
        f"{exchange.response_body or exchange.response_text!r}"
    )
    is_object = isinstance(exchange.response_body, dict)
    journal.record(
        "response_json_object", type(exchange.response_body).__name__, "a JSON object", ok=is_object
    )
    assert is_object, "expected a JSON response object"

    return exchange.response_body


def check_stream_opened(exchange: Exchange, ingress: Ingress) -> None:
    """Check transport, throttling and status before reading a stream's frames.

    The streaming counterpart to `check_json_response`. Streaming cases do not go through that
    one -- `client.stream()` returns an exchange and a transcript rather than a body -- so without
    this they each repeated the status assertion and none of them understood a 429.
    """
    assert_not_throttled(exchange, ingress)
    journal.record(
        "stream_transport_error",
        exchange.transport_error,
        "none",
        ok=exchange.transport_error is None,
    )
    assert exchange.transport_error is None, f"stream failed: {exchange.transport_error}"
    journal.record("http_status", exchange.status, "200", ok=exchange.status == 200)
    assert exchange.status == 200, (
        f"expected 200 from {ingress.id!r}, got {exchange.status}: "
        f"{exchange.response_body or exchange.response_text!r}"
    )


def check_sse_response(exchange: Exchange, ingress: Ingress) -> None:
    """Check the transport and media type before a surface validates its SSE grammar."""
    check_stream_opened(exchange, ingress)
    content_type = (
        exchange.response_headers.get("content-type", "").split(";", 1)[0].strip().lower()
    )
    journal.record(
        "stream_content_type",
        content_type,
        "text/event-stream",
        ok=content_type == "text/event-stream",
    )
    assert content_type == "text/event-stream", f"expected an SSE response, got {content_type!r}"


@contextmanager
def throttle_aware(ingress: Ingress):
    """Translate an SDK's rate-limit error into the same classified skip as a raw 429.

    SDK cases raise rather than returning an exchange, so neither `check_json_response` nor
    `check_stream_opened` sees them. Without this a declared throttle would skip the raw cases of
    a surface and fail its SDK case, in the same run, for the same reason.

    Matched on the status the exception carries rather than on an SDK exception class, so this
    works for both pinned SDKs and does not need either imported here. Anything that is not a 429
    is re-raised untouched.
    """
    try:
        yield
    except Exception as exc:
        if getattr(exc, "status_code", None) != 429 or not ingress.rate_limited:
            raise
        journal.record("throttled_via_sdk", 429, "declared rate_limited", ok=True)
        _skip_throttled(f"ingress {ingress.id!r} is declared rate_limited and the SDK saw a 429")


def check_completion(
    completion: Completion, surface: Surface, model: Model, *, quotes_markup: bool = False
) -> None:
    """Check the same generation contract after raw HTTP or SDK deserialisation."""
    check_choice(completion, surface, model, quotes_markup=quotes_markup)
    assert_usage_present_and_consistent(completion)


def check_choice(
    completion: Completion, surface: Surface, model: Model, *, quotes_markup: bool = False
) -> None:
    surface.native_contract(completion.raw)
    check_identity(completion, surface, model)
    assert_healthy_finish(completion)
    assert_has_output(completion, model)
    check_call_markup(completion, surface, model, quoted=quotes_markup)


def check_call_markup(
    completion: Completion, surface: Surface, model: Model, *, quoted: bool = False
) -> None:
    """No generation on a tool surface carries the model's declared call markup in its answer.

    Part of the shared contract, so a leak fails the case it happens in, whether or not that
    case expected a call. `quoted` is for the one case whose prompt asks for the markup.
    """
    if "tools" in surface.features:
        assert_no_call_markup(completion, model, quoted=quoted)


def check_identity(completion: Completion, surface: Surface, model: Model) -> None:
    object_type = surface.stream_object_type if completion.streamed else surface.object_type
    role = surface.role
    journal.record(
        "object", completion.object_type, object_type, ok=completion.object_type == object_type
    )
    if role is not None:
        journal.record("role", completion.role, role, ok=completion.role == role)

    assert completion.object_type == object_type, (
        f"expected object={object_type!r}, got {completion.object_type!r}"
    )
    if role is not None:
        assert completion.role == role, f"expected role={role!r}, got {completion.role!r}"
    check_model_echo(completion, model)


def check_model_echo(completion: Completion, model: Model) -> None:
    """A different ingress name is legitimate; a different response model is not."""
    journal.record(
        "model_echo",
        completion.model_echo,
        model.public_id,
        ok=completion.model_echo == model.public_id,
    )

    assert completion.model_echo == model.public_id, (
        f"response echoed model {completion.model_echo!r}, expected {model.public_id!r} -- "
        f"a different model answered than was asked for"
    )


def _sdk_secret(request, ingress: Ingress) -> str:
    """Use only the plugin's resolved secret; never trigger SDK credential discovery."""
    return getattr(request.config, "_llmdv_secrets", {}).get(ingress.id) or "not-required"


@pytest.fixture
def openai_client(request, ingress):
    """The pinned OpenAI SDK pointed at one ingress, with its traffic recorded.

    Used only where SDK compatibility is itself the requirement. Everything else goes through
    raw httpx, because the SDK normalises away most of what needs proving -- malformed JSON,
    wrong types, wrong methods, header manipulation.

    The SDK is handed a hooked transport so these cases leave the same exchange evidence as
    every other one. Without it their verdict would rest on nothing a reviewer could inspect.
    """
    openai = pytest.importorskip("openai", reason="pinned OpenAI SDK not installed")

    from ..http.client import SdkRecorder
    from ..plugin import _correlation_headers, register_client

    timeout = httpx.Timeout(ingress.timeouts.request, connect=ingress.timeouts.connect)
    recorder = SdkRecorder(
        httpx.Client(
            timeout=timeout,
            trust_env=ingress.setup.proxy is ProxyMode.ALLOW,
            verify=ingress.tls.verify,
        )
    )
    recorder.client.headers.update(_correlation_headers(request.config, request.node))
    register_client(request.node, ingress, recorder)
    client = openai.OpenAI(
        base_url=f"{ingress.base_url}/v1",
        api_key=_sdk_secret(request, ingress),
        max_retries=0,  # a retry would mask the very failure under test
        timeout=timeout,
        http_client=recorder.client,
    )
    try:
        yield client
    finally:
        recorder.close()


@pytest.fixture
def anthropic_client(request, ingress, surface, monkeypatch):
    """Pinned Messages SDK with profile-selected credentials and recorded exchanges."""
    import anthropic

    from ..http.client import SdkRecorder
    from ..plugin import _correlation_headers, register_client

    secret = _sdk_secret(request, ingress)
    auth = {"auth_token": secret} if ingress.auth.type is AuthType.BEARER else {"api_key": secret}
    # This SDK also accepts arbitrary headers from the environment. The profile must decide
    # authentication, so ambient SDK headers must not override its declared credentials.
    monkeypatch.delenv("ANTHROPIC_CUSTOM_HEADERS", raising=False)
    timeout = anthropic.Timeout(ingress.timeouts.request, connect=ingress.timeouts.connect)
    recorder = SdkRecorder(
        anthropic.DefaultHttpxClient(
            timeout=timeout,
            trust_env=ingress.setup.proxy is ProxyMode.ALLOW,
            verify=ingress.tls.verify,
        )
    )
    recorder.client.headers.update(_correlation_headers(request.config, request.node))
    register_client(request.node, ingress, recorder)
    try:
        yield anthropic.Anthropic(
            base_url=ingress.base_url,  # the SDK appends /v1/messages
            **auth,
            max_retries=0,
            timeout=timeout,
            default_headers=surface.extra_headers(),
            http_client=recorder.client,
        )
    finally:
        recorder.close()
