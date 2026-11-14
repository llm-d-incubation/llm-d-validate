"""API surfaces: the wire shape of a request and response, independent of transport.

A surface knows paths, bodies and grammar. An ingress knows base URL, auth and timeouts.
Keeping those orthogonal is what lets one logical case run through three layers unchanged,
which is the whole basis of layer attribution.

`Completion` is the normalisation that makes a shared assertion library possible. OpenAI
chat completions report `finish_reason` with `usage.prompt_tokens`; Anthropic messages
report `stop_reason` with `usage.input_tokens`. Normalising once here means
`assert_healthy_completion` is written a single time rather than per surface.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Protocol, TypeVar

if TYPE_CHECKING:
    from ..http.sse import Transcript
    from .cache import CacheUsage


_REGISTRY: dict[str, WireSurface] = {}
_SurfaceT = TypeVar("_SurfaceT", bound="WireSurface")


@dataclass(frozen=True)
class Usage:
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    reasoning_tokens: int | None = None

    @property
    def is_self_consistent(self) -> bool:
        """Whether prompt + completion == total, when all three are present.

        True when any is missing: absence is a separate assertion, and conflating the two
        makes a missing-usage failure read as an arithmetic one.
        """
        if None in (self.prompt_tokens, self.completion_tokens, self.total_tokens):
            return True
        return self.prompt_tokens + self.completion_tokens == self.total_tokens


@dataclass(frozen=True)
class ToolSpec:
    """A function definition, which each surface renders in its own tool shape."""

    name: str
    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True)
class ToolCall:
    """A function invocation; malformed native values remain visible to assertions."""

    id: Any
    name: Any
    arguments: Any
    index: int = 0
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Completion:
    """One generation, normalized across surfaces with its observed transport shape."""

    role: str | None
    text: str
    finish_reason: str | None
    usage: Usage = field(default_factory=Usage)
    reasoning_text: str = ""
    reasoning_source: str | None = None
    reasoning_carrier: str | None = None
    streamed: bool = False
    assistant_message: dict[str, Any] = field(default_factory=dict)
    tool_calls: tuple[ToolCall, ...] = ()
    object_type: str | None = None
    model_echo: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def has_output(self) -> bool:
        """Any generated output at all, counting reasoning and tool calls.

        Reasoning counts deliberately. On a fleet served without `--reasoning-parser` the
        model's whole budget can go into a `<think>` block, so "content is non-empty" is the
        wrong question; "did it generate anything" is the right one.
        """
        return bool(self.text.strip() or self.reasoning_text.strip() or self.tool_calls)


@dataclass(frozen=True)
class Chunk:
    """One streamed frame, normalised the same way `Completion` is.

    `is_terminal_usage` marks the frame that `stream_options.include_usage` adds: it carries
    an empty `choices` array and the usage totals, and arrives after the finish-reason frame.
    Distinguishing it matters because "a frame with no choices" is otherwise indistinguishable
    from a malformed one.
    """

    role: str | None = None
    content_delta: str = ""
    reasoning_delta: str = ""
    finish_reason: str | None = None
    usage: Usage | None = None
    has_choices: bool = True
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_terminal_usage(self) -> bool:
        return self.usage is not None and not self.has_choices


@dataclass(frozen=True)
class ErrorEnvelope:
    """A surface-specific error body, reduced to the fields worth asserting on.

    Error prose is deliberately not compared. The task's requirement is the status and the
    envelope shape, and asserting on wording makes the suite fail on a harmless upstream
    message change.
    """

    type: str | None
    message: str | None
    code: str | int | None = None
    param: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


TOOL_NAME = "llmdv_echo"

DEFAULT_PROMPT = "Reply with the single word: ok"
# The answer to DEFAULT_PROMPT, for the cases that check the answer channel itself.
DEFAULT_ANSWER = "ok"
# A sentence left open, so a model with no chat template continues it rather than ending. A
# raw instruction like DEFAULT_PROMPT has no cue there, and ending at once is a fair reply.
OPEN_PROMPT = "The capital of France is"
# A question a model reasons about, whose answer channel must then carry the number alone.
REASONING_PROMPT = "What is 17 times 23? Reply with the number only."
REASONING_ANSWER = "391"

# Error classes a surface must be able to express as a request body, because each one is
# expressed differently per wire shape: the required field is `messages` on one surface and
# `input` on another, and the token limit is `max_tokens` on one and `max_output_tokens` on
# another. A case that hand-builds these from one surface's shape sends a garbled request to
# every other surface -- one that is either refused for the wrong reason, so it passes, or
# ignored, so it fails and blames the fleet.
REQUEST_ERROR_CLASSES = (
    "missing_required_field",
    "wrong_field_type",
    "invalid_token_limit",
)
# Only meaningful on a surface whose `features` include "tools".
TOOL_ERROR_CLASSES = ("invalid_tool_schema", "invalid_tool_choice")

# Every feature a surface may declare and a surface-generic case may require. A closed
# vocabulary, because a misspelt feature would otherwise match no surface and silently select
# nothing.
SURFACE_FEATURES = frozenset(
    {
        "cache_usage",
        "image_input",
        "json_object",
        "reasoning_usage",
        "render",
        "structured_output",
        "tool_loop",
        "tools",
        "vllm_options",
    }
)


class SurfaceKind(str, Enum):
    GENERATION = "generation"
    RENDER = "render"


class WireSurface(Protocol):
    """Request/error contract shared by generation and render APIs."""

    kind: SurfaceKind
    name: str
    path: str
    features: frozenset[str]

    def minimal_body(
        self, model: str, max_tokens: int = 16, prompt: str = DEFAULT_PROMPT, **extra: Any
    ) -> dict[str, Any]: ...
    def extra_headers(self) -> dict[str, str]: ...
    def parse_error(self, body: Any) -> ErrorEnvelope | None: ...
    def looks_like_success(self, body: Any) -> bool: ...


class Surface(WireSurface, Protocol):
    """Generation API contract; render APIs implement WireSurface instead."""

    name: str
    path: str
    # Expected response shape. None means this surface has no top-level role contract.
    kind = SurfaceKind.GENERATION
    object_type: str
    stream_object_type: str
    stream_stop: str | frozenset[str] | None = None
    has_stream_lifecycle: bool = True  # assert_stream is implemented.
    role: str | None
    requires_capability: str | None
    # Optional request features this wire shape has, e.g. "tools". A surface-generic case that
    # needs one runs only on the surfaces that have it.
    features: frozenset[str]
    # A prompt a correct model always answers with text on this wire, for the cases that require
    # output. The default prompt stays the default, so prompt and render pins keep their text.
    reply_prompt: str = DEFAULT_PROMPT

    def minimal_body(
        self, model: str, max_tokens: int = 16, prompt: str = DEFAULT_PROMPT, **extra: Any
    ) -> dict[str, Any]: ...

    def structured_output_body(
        self, model: str, schema: dict[str, Any], max_tokens: int, prompt: str
    ) -> dict[str, Any]:
        """Feature-gated: implemented only by surfaces declaring structured_output.

        Callers must gate on that feature; this method is not required of other surfaces.
        """
        ...

    def image_body(self, model: str, max_tokens: int, prompt: str, *pngs: bytes) -> dict[str, Any]:
        """Feature-gated: a user turn carrying PNGs in order, then `prompt`, on image_input
        surfaces."""
        ...

    def image_url_body(self, model: str, max_tokens: int, prompt: str, url: str) -> dict[str, Any]:
        """Feature-gated: the same turn with one image named by URL for the fleet to fetch."""
        ...

    def tools_body(
        self, model: str, max_tokens: int, prompt: str, tools: list[ToolSpec], **extra: Any
    ) -> dict[str, Any]:
        """Feature-gated: a request offering `tools`, on surfaces declaring tools.

        No tool choice is set, so the surface default applies.
        """
        ...

    def invalid_body(self, model: str, error_class: str, stream: bool = False) -> dict[str, Any]:
        """A request that must be refused as `error_class`, in this surface's own shape.

        Covers `REQUEST_ERROR_CLASSES`, plus `TOOL_ERROR_CLASSES` when the surface has tools.
        """
        ...

    def extra_headers(self) -> dict[str, str]: ...

    def native_contract(self, raw: dict[str, Any]) -> None:
        """Additional native response checks beyond the normalized completion."""

    def parse_cache_usage(self, body: dict[str, Any]) -> CacheUsage:
        """Feature-gated: native cache counters of a response, on surfaces with cache_usage."""
        ...

    def stream_cache_usage(self, transcript: Transcript) -> CacheUsage:
        """Feature-gated: the same counters, read where a streaming client reads them."""
        ...

    def assert_stream(self, transcript: Transcript) -> None:
        raise NotImplementedError(f"{self.name} has no stream lifecycle assertion")

    def parse_completion(self, body: dict[str, Any]) -> Completion: ...

    def parse_error(self, body: Any) -> ErrorEnvelope | None: ...

    def looks_like_success(self, body: Any) -> bool: ...

    def stream_body(
        self, model: str, max_tokens: int = 16, include_usage: bool = False, **extra: Any
    ) -> dict[str, Any]: ...

    def parse_chunk(self, body: dict[str, Any]) -> Chunk: ...

    def assemble(self, chunks: list[Chunk]) -> Completion: ...


def register(surface: _SurfaceT) -> _SurfaceT:
    unknown = surface.features - SURFACE_FEATURES
    if unknown:
        raise ValueError(
            f"surface {surface.name!r} declares unknown features {sorted(unknown)}; "
            f"known: {sorted(SURFACE_FEATURES)}"
        )
    _REGISTRY[surface.name] = surface
    return surface


def get(name: str) -> WireSurface:
    try:
        return _REGISTRY[name]
    except KeyError:
        raise KeyError(f"no surface implementation named {name!r}") from None


def implemented() -> frozenset[str]:
    """Surface names this build can actually drive.

    The loader uses this to warn about a declared-but-unimplemented surface, and the plugin
    uses it to skip those cases as `not_implemented` -- distinct from the fleet not exposing
    them, which is `undeclared`.
    """
    return frozenset(_REGISTRY)


# Surface names that are declarable in a profile, and the path each one lives at. Wider than
# `implemented()` on purpose, so a profile can describe a fleet honestly before llmdv can drive
# every surface it exposes -- and so the contract suite can probe for a route's existence
# without needing a full implementation behind it.
SURFACE_PATHS = {
    "chat_completions": "/v1/chat/completions",
    "completions": "/v1/completions",
    "messages": "/v1/messages",
    "responses": "/v1/responses",
    "chat_render": "/v1/chat/completions/render",
    "completions_render": "/v1/completions/render",
    "messages_render": "/v1/messages/render",
    "responses_render": "/v1/responses/render",
}

KNOWN_SURFACES = frozenset(SURFACE_PATHS)


# Minimal VALID bodies for probing whether a route exists.
#
# An empty `{}` is not usable for this, and finding that out cost a false positive. The llm-d
# router gateway validates a request body before it resolves the route, so `{}` comes back 400
# from the gateway's own validation while the same path with a real body comes back 404 --
# measured live, where /v1/chat/completions/render answers 400 to `{}` and 404 to a
# valid body. A probe reading "not 404 means present" therefore reports render surfaces as
# served when they are absent.
#
# A valid request proves presence only on a 2xx answer; gateway errors do not.
def probe_request(surface: str, model: str) -> tuple[dict, dict[str, str]]:
    """A (body, headers) pair that is valid enough to reach route resolution."""
    anthropic = {"anthropic-version": "2023-06-01"}
    messages = [{"role": "user", "content": "ping"}]

    if surface in ("chat_completions", "chat_render"):
        return {"model": model, "messages": messages, "max_tokens": 1}, {}
    if surface in ("completions", "completions_render"):
        return {"model": model, "prompt": "ping", "max_tokens": 1}, {}
    if surface in ("messages", "messages_render"):
        return {"model": model, "max_tokens": 1, "messages": messages}, anthropic
    if surface in ("responses", "responses_render"):
        return {"model": model, "input": "ping", "max_output_tokens": 16}, {}
    return {"model": model}, {}
