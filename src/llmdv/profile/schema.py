"""The infra-profile: a declarative description of one deployed fleet, from one vantage point.

A profile is hand-written and committed to git. It carries no secrets -- `auth.key_env` names
an environment variable instead.

Two vocabularies in here carry most of the design's weight.

`Disposition` is what a layer does to a header. The same header has three different
dispositions depending on where it enters an llm-d stack: LiteLLM's flow-control hook
strips the client's `x-llm-d-inference-objective` and rewrites it from `objectives.yaml`, the
EPP consumes it, and vLLM ignores it. Only some of those are observable from outside, which
is what `verifiable_by` records -- an unobservable one must skip with a reason, never pass.

`SkipClass` keeps six reasons distinguishable. A matrix that renders "the fleet does not
support this" and "llmdv cannot test this yet" as the same grey box is worse than no matrix,
because it reads as coverage.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from .fleet import FleetChecks, MetricsSource, Signature
from .transport import TRANSPORT_ERROR_CLASSES, TransportContract


class IngressKind(str, Enum):
    VLLM = "vllm"
    LLMD_ROUTER = "llmd_router"
    LITELLM = "litellm"


# How deep each kind sits in a stack, outermost first. Determined rather than declared: vLLM
# generates rather than forwarding, so it is terminal, and LiteLLM is the front door. That is
# why there is no topology field.
#
# Two ingresses of the same kind tie, and `sorted` being stable means they keep declaration
# order. No profile has needed that; a second gateway or a second router would be the first,
# and would be the point to add an explicit ordering here.
_KIND_DEPTH = {IngressKind.LITELLM: 0, IngressKind.LLMD_ROUTER: 1, IngressKind.VLLM: 2}


def depth(kind: IngressKind | str) -> int:
    """How deep a kind sits, 0 outermost: the order `Profile.chain` reads the layers in."""
    return _KIND_DEPTH[IngressKind(kind)]


class AuthType(str, Enum):
    NONE = "none"
    BEARER = "bearer"
    API_KEY = "x-api-key"


class Vantage(str, Enum):
    """Where the client process ran, relative to the cluster.

    The distinction is positional, not provenance: a CI runner outside the cluster is
    `external` exactly as a laptop is, and a CI job that runs llmdv in a pod is `in-cluster`.
    What drove the run belongs in `--label`, which records it separately.

    It is the one thing the rest of the profile cannot imply. A pod calling the fleet's own
    external Route declares the same `base_url` as a laptop, needs no `setup.requires`, and
    sets the same proxy mode -- yet differs in latency floor, proxy path and failure modes.
    """

    EXTERNAL = "external"
    IN_CLUSTER = "in-cluster"

    @classmethod
    def _missing_(cls, value: object) -> Vantage | None:
        # `laptop` was the original name for this position, and it is wrong the moment the
        # client is a CI runner. Accepted so existing profiles keep loading.
        if isinstance(value, str) and value.strip().lower() == "laptop":
            return cls.EXTERNAL
        return None


class ProxyMode(str, Enum):
    BYPASS = "bypass"
    ALLOW = "allow"


class Verifiable(str, Enum):
    STATUS = "status"
    RESPONSE_HEADERS = "response_headers"
    INSTRUMENTED_BACKEND = "instrumented_backend"
    LOAD_EXPERIMENT = "load_experiment"


class Disposition(str, Enum):
    IGNORED = "ignored"
    CONSUMED = "consumed"
    FORWARDED = "forwarded"
    STRIPPED = "stripped"
    REWRITTEN = "rewritten"
    ECHOED = "echoed"
    REJECTED = "rejected"
    PROMISED = "promised"
    ABSENT = "absent"

    @property
    def verifiable_by(self) -> Verifiable:
        return _VERIFIABLE_BY[self]

    @property
    def client_observable(self) -> bool:
        return self.verifiable_by in (Verifiable.STATUS, Verifiable.RESPONSE_HEADERS)


_VERIFIABLE_BY = {
    Disposition.IGNORED: Verifiable.STATUS,
    Disposition.CONSUMED: Verifiable.LOAD_EXPERIMENT,
    Disposition.FORWARDED: Verifiable.INSTRUMENTED_BACKEND,
    Disposition.STRIPPED: Verifiable.INSTRUMENTED_BACKEND,
    Disposition.REWRITTEN: Verifiable.INSTRUMENTED_BACKEND,
    Disposition.ECHOED: Verifiable.RESPONSE_HEADERS,
    Disposition.REJECTED: Verifiable.STATUS,
    Disposition.PROMISED: Verifiable.RESPONSE_HEADERS,
    Disposition.ABSENT: Verifiable.RESPONSE_HEADERS,
}


class SkipClass(str, Enum):
    UNDECLARED = "undeclared"
    NOT_IMPLEMENTED = "not_implemented"
    REQUIRES_INSTRUMENTATION = "requires_instrumentation"
    REQUIRES_LOAD = "requires_load"
    # Not a gap in the fleet or the tool: the case could run and we chose not to, because the
    # profile says this fleet is shared. A hand-typed `-k` exclusion expresses the same intent
    # invisibly, which is why this is a class rather than a convention -- the row stays in the
    # matrix and the run records what it declined to do.
    EXCLUDED_BY_POLICY = "excluded_by_policy"
    # The layer refused to serve us because we are over our own quota. Not a fleet defect, and
    # not a pass: the case could not be exercised. Only ever used where the ingress declares
    # `rate_limited: true`, so an undeclared 429 stays a failure.
    THROTTLED = "throttled"

    @classmethod
    def for_disposition(cls, disposition: Disposition) -> SkipClass | None:
        """Which skip class an unobservable disposition maps onto."""
        return {
            Verifiable.INSTRUMENTED_BACKEND: cls.REQUIRES_INSTRUMENTATION,
            Verifiable.LOAD_EXPERIMENT: cls.REQUIRES_LOAD,
        }.get(disposition.verifiable_by)


# Error classes a surface can be asked to fail correctly for. Only `unknown_model` is
# consumed in this pass; the vocabulary is fixed now so profiles stay forward-compatible.
ERROR_CLASSES = frozenset(
    {
        "unknown_model",
        "malformed_json",
        "missing_required_field",
        "wrong_field_type",
        "context_overflow",
        "output_overflow",
        "invalid_token_limit",
        "invalid_tool_schema",
        "invalid_tool_choice",
        "orphan_tool_result",
        "internal_image_url",
        "unsupported_option",
        "bad_credentials",
        "invalid_api_version",
        "invalid_field_value",
        "unknown_path",
        "wrong_method",
        "streaming_validation",
        "unknown_response",
        "unsupported_feature",
    }
)
ERROR_CLASSES = ERROR_CLASSES | TRANSPORT_ERROR_CLASSES


@dataclass(frozen=True)
class Environment:
    """Where the fleet lives, and how to look at it with a kubeconfig.

    Entirely optional: a profile with only `base_url`s still works, which keeps llmdv usable
    against an endpoint whose cluster you cannot reach. When it IS present, preflight can
    resolve a `setup.requires` selector to a live pod name and tell you that your
    port-forward target was rescheduled -- rather than letting you debug a connection
    refused that has nothing to do with the fleet.
    """

    cluster: str | None = None
    context: str | None = None
    namespace: str | None = None
    kubeconfig: Path | None = None
    # A read-only metrics API for the fleet suite's metrics cases. None when not declared.
    metrics: MetricsSource | None = None


@dataclass(frozen=True)
class Reasoning:
    """How reasoning text reaches the client, if at all.

    `parser` is None on a fleet served without `--reasoning-parser`. That is not a
    degenerate case: a fleet served without that flag is exactly this, and its reasoning
    arrives as a literal `<think>` block inside `content` with `reasoning_content` null. A
    test asserting "content is non-empty" passes there for the wrong reason, and one
    asserting `reasoning_content` is populated fails against a working stack -- so the
    shape has to be declared rather than assumed.
    """

    parser: str | None = None
    field_name: str | None = None
    inline_tag: str | None = None
    default_on: bool = False

    @property
    def is_separate_field(self) -> bool:
        return self.field_name is not None

    @property
    def is_inline(self) -> bool:
        return self.inline_tag is not None


# Chat `finish_reason` values that end a forced named-function call. OpenAI's own API answers
# `stop`; some OpenAI-compatible stacks answer `tool_calls`. Neither is a defect, so it is declared.
CHAT_FORCED_TOOL_FINISHES = ("tool_calls", "stop")


# vLLM's --tool-strict-level: `auto` and `function` constrain the call envelope only, so a tool's
# argument schema is enforced only when that tool is sent with `strict: true`; `parameter` pins
# every tool's schema as if each were strict. OpenAI makes the same promise: schema adherence is
# a `strict` feature. The cases assert argument validity only where the declared level promises it.
TOOL_STRICT_LEVELS = ("auto", "function", "parameter")


@dataclass(frozen=True)
class Tools:
    parser: str | None = None
    auto_choice: bool = False
    forced_finish_reason: str = "tool_calls"
    strict_level: str = "auto"
    # Raw call markup the model writes before the parser runs, such as a call tag. Answer text
    # that carries any of these strings holds a call the parser missed or leaked.
    markup: tuple[str, ...] = ()

    @property
    def supported(self) -> bool:
        return self.parser is not None

    @property
    def enforces_every_schema(self) -> bool:
        """Whether the server pins every tool's argument schema, strict or not."""
        return self.strict_level == "parameter"


@dataclass(frozen=True)
class Model:
    public_id: str
    aliases: tuple[str, ...] = ()
    max_model_len: int | None = None
    reasoning: Reasoning = field(default_factory=Reasoning)
    tools: Tools = field(default_factory=Tools)
    # Opt-in sufficiency contract for a simple prompt in the deployed default reasoning mode.
    answer_max_tokens: int | None = None

    @property
    def all_names(self) -> tuple[str, ...]:
        return (self.public_id, *self.aliases)


@dataclass(frozen=True)
class Auth:
    type: AuthType = AuthType.NONE
    key_env: str | None = None

    def header(self, secret: str | None) -> dict[str, str]:
        if self.type is AuthType.NONE or not secret:
            return {}
        if self.type is AuthType.BEARER:
            return {"Authorization": f"Bearer {secret}"}
        return {"x-api-key": secret}


class FleetPolicy(str, Enum):
    """Whether this fleet is ours to abuse.

    `dedicated` is the default because it preserves the behaviour every existing profile was
    written against: everything runs. `shared` is an explicit statement that other people's
    traffic, measurements or dashboards are on this fleet, and that cases which cost it real work
    or send deliberately malformed input should not run.

    It is declared rather than inferred. Nothing about a URL, an ingress kind or a namespace tells
    you whether someone is relying on what is behind it.
    """

    DEDICATED = "dedicated"
    SHARED = "shared"


@dataclass(frozen=True)
class Timeouts:
    connect: float = 5.0
    request: float = 600.0
    stream_idle: float = 60.0


@dataclass(frozen=True)
class Requirement:
    """One precondition for reaching an ingress from this vantage point.

    `selector` is preferred over a literal pod name because pod names rot -- a decode pod
    rescheduled overnight leaves a profile pointing at something that no longer exists. With
    a kubeconfig, llmdv resolves the selector and prints a command with the current name.
    """

    kind: str
    local_port: int | None = None
    remote_port: int | None = None
    namespace: str | None = None
    target: str | None = None
    selector: str | None = None
    command: str | None = None


@dataclass(frozen=True)
class Setup:
    proxy: ProxyMode = ProxyMode.BYPASS
    requires: tuple[Requirement, ...] = ()


@dataclass(frozen=True)
class Tls:
    # Off only per ingress and only by declaration, e.g. for a cluster's private ingress CA.
    # Other connections (object storage, Slack) always verify.
    verify: bool = True


@dataclass(frozen=True)
class LogSource:
    """Where an ingress's own account of a request lives.

    Declared per ingress because the pods differ by layer -- the engine's logs explain a 500,
    the EPP's explain a routing decision, LiteLLM's explain a transformation -- and a single
    namespace-wide dump would bury all three. When absent, llmdv falls back to whatever the
    port-forward requirement already names, so the common case needs no extra configuration.
    """

    selector: str | None = None
    target: str | None = None
    container: str | None = None
    tail: int = 2000
    # Lines that must not appear in the run window, and patterns that excuse a matched line.
    # Scanned by the `logs.signatures` case; collection alone ignores them.
    forbid: tuple[Signature, ...] = ()
    allow: tuple[str, ...] = ()


@dataclass(frozen=True)
class Ingress:
    id: str
    kind: IngressKind
    base_url: str
    # Fully resolved by the loader, including inherited profile defaults.
    model: Model
    capabilities: Capabilities
    auth: Auth = field(default_factory=Auth)
    # The environment variable that holds `base_url`, for a profile that is committed: a real
    # gateway hostname is cluster-identifiable and stays out of the repository, like the key.
    base_url_env: str | None = None
    health_path: str = "/health"
    surfaces: tuple[str, ...] = ()
    timeouts: Timeouts = field(default_factory=Timeouts)
    error_status: dict[str, tuple[int, ...]] = field(default_factory=dict)
    # Whether this layer may refuse us for being over quota. A gateway key with a deliberately
    # small rate limit is the normal case: the 429 is the limit working, not the fleet failing.
    # Declared, because an undeclared 429 from a layer we did not expect to throttle is a real
    # finding and must stay a failure.
    rate_limited: bool = False
    # Error classes this layer rejects WITHOUT a structured envelope, as a declared deviation.
    #
    # A gateway that validates a request before forwarding it tends to answer in its own words
    # rather than in the upstream API's error shape, so a client SDK cannot parse the body. That
    # is a real contract gap, and declaring it is how the suite keeps it visible instead of
    # either going permanently red or quietly ignoring it: the case asserts the ABSENCE of an
    # envelope for a declared class, so the declaration fails if the layer is fixed, and the
    # ordinary assertion fails if a layer regresses into this state.
    unstructured_errors: tuple[str, ...] = ()
    setup: Setup = field(default_factory=Setup)
    logs: tuple[LogSource, ...] = ()
    tls: Tls = field(default_factory=Tls)
    # Declared HTTP behavior; see profile/transport.py.
    transport: TransportContract = field(default_factory=TransportContract)

    @property
    def log_sources(self) -> tuple[LogSource, ...]:
        """Declared log sources, or the port-forward targets as a fallback.

        The fallback is what makes this work without anyone editing a profile: a requirement
        already names the pod or Service the traffic goes to, which is the same place its logs
        come from.
        """
        if self.logs:
            return self.logs
        return tuple(
            LogSource(selector=requirement.selector, target=requirement.target)
            for requirement in self.setup.requires
            if requirement.selector or requirement.target
        )

    def expected_error_status(self, error_class: str) -> tuple[int, ...] | None:
        """Statuses acceptable for an error class, or None meaning "any 4xx"."""
        return self.error_status.get(error_class) or None

    def exposes(self, surface: str) -> bool:
        return surface in self.surfaces

    def produces_envelope(self, error_class: str) -> bool:
        """Whether this layer is expected to answer this error class with a parseable envelope."""
        return error_class not in self.unstructured_errors


# How a repeat reaches the replica that holds its prefix, beyond `single_replica` and
# `replica_header`. `routed`: placing the repeat on a warm replica is the ingress's own job, as
# for a prefix-aware router or a gateway in front of one, so a miss is a failure.
CACHE_AFFINITIES = ("routed",)


@dataclass(frozen=True)
class CacheReuse:
    min_ratio: float
    surfaces: tuple[str, ...] = ("chat_completions",)
    prefix_words: int = 1024
    single_replica: bool = False
    replica_header: str | None = None
    affinity: str | None = None
    # The engine's KV block size in tokens. When declared, a warm repeat must read every whole
    # block before its last prompt token and a fresh prefix must read none; when absent, the
    # declared ratios decide.
    block_size: int | None = None
    # Trailing blocks of every hit the deployment withholds by design, as an engine with a
    # speculative-decoding draft recomputes the draft's KV for the last chunk of a hit. Lowers
    # the block floors by that many blocks; a warm read of nothing still fails.
    withheld_blocks: int = 0
    # The template renders a tool schema's keys in a fixed order, so a client that rebuilds the
    # same schema with its keys in another order sends the same prompt.
    canonical_tool_keys: bool = False

    cold_max_ratio: float | None = None
    # Cache surfaces whose templates render one plain conversation alike, so a request on one
    # reads the prefix another computed.
    render_equivalent: tuple[str, ...] = ()
    # A leading system line the fleet removes before rendering, as some stacks do with a client's
    # per-request attribution line, so the value it carries cannot break reuse.
    stripped_system_prefix: str | None = None
    # Pairs of `surface:setting` whose templates render one conversation alike, such as a
    # Messages thinking mode and a Chat effort, so a request with one reads the other's prefix.
    # A Messages `enabled` entry may carry its thinking budget, `messages:enabled:2048`.
    equivalent_settings: tuple[tuple[str, str], ...] = ()
    # Ingress ids in front of the same engines and prefix cache, so a prefix computed through
    # one is read through another.
    shared_with: tuple[str, ...] = ()


CHAT_REASONING_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
MESSAGES_THINKING_MODES = ("enabled", "adaptive", "disabled")
CLIENT_SESSIONS = (
    "skill",
    "hook",
    "parallel_reads",
    "nested_subagent",
    "compaction",
    "multi_file_fix",
    "image_read",
)
MESSAGES_EFFORTS = ("low", "medium", "high", "xhigh", "max")
# Opt-in Messages cases that have no other capability to hang on.
MESSAGES_CHECKS = (
    "context_management",
    "effort_effect",
    "incremental_streaming",
    "mid_conversation_system",
    "parallel_tool_calls",
    "prompt_accounting",
    "error_types",
    "max_tokens_limit",
    "stop_sequences",
    "response_shape",
    "count_tokens_parts",
    "block_forms",
    "tool_result_error",
)
# Opt-in session and scenario cases: shapes a fleet may legitimately refuse, or that cost it real
# work, so none runs until the profile names it.
SCENARIO_CHECKS = (
    "blank_turns",
    "cache_hints",
    "inline_system",
    "long_context",
    "output_overflow",
    "parallel_subagents",
)
# Named Messages request features a fleet either serves or refuses. A profile declares the
# outcome per feature under `messages_features`; an undeclared feature is not sent.
MESSAGES_FEATURES = (
    "assistant_prefill",
    "cache_breakpoints_five",
    "cache_control_message_block",
    "cache_control_tool",
    "cache_control_ttl_1h",
    "consecutive_assistant_turns",
    "consecutive_user_turns",
    "container",
    "diagnostics",
    "document_citations",
    "document_content",
    "document_pdf",
    "document_text",
    "image_base64",
    "image_url",
    "inference_geo",
    "leading_assistant",
    "max_tokens_large",
    "max_tokens_over_window",
    "max_tokens_zero",
    "mcp_servers",
    "metadata_user_id",
    "redacted_thinking_passback",
    "search_result",
    "server_tool_history",
    "service_tier",
    "speed_fast",
    "stop_sequences_five",
    "text_citations_passback",
    "thinking_between_tools",
    "thinking_display_omitted",
    "thinking_display_updates",
    "tool_result_blocks",
    "tool_result_image",
    "top_k",
    "top_level_cache_control",
    "top_p",
)
MESSAGES_FEATURE_OUTCOMES = ("accepted", "rejected")
RESPONSES_REASONING_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
RESPONSES_OPTIONS = ("metadata", "logprobs")
CHAT_OPTIONS = ("n", "logprobs")
# The reasoning setting each cache surface carries, by the values its field takes: Chat
# `reasoning_effort`, Messages `thinking.type` and Responses `reasoning.effort`.
SETTING_VALUES = {
    "chat_completions": CHAT_REASONING_EFFORTS,
    "messages": MESSAGES_THINKING_MODES,
    "responses": RESPONSES_REASONING_EFFORTS,
}
# The least thinking budget Messages `enabled` takes, and the one an `equivalent_settings`
# entry gets when it names none. A gateway can map the budget onto an effort, so an entry may
# carry its own as `messages:enabled:<budget>`.
MESSAGES_MIN_THINKING_BUDGET = 1024


def parse_setting(entry: Any) -> tuple[str, str, int | None] | None:
    """A `surface:setting[:budget]` entry as surface, setting and budget, or None if malformed.

    The budget is set only for Messages `enabled`, where it defaults to the least the API takes.
    No surface or value is checked against the declared ones here.
    """
    if not isinstance(entry, str):
        return None
    name, _, rest = entry.partition(":")
    value, colon, budget = rest.partition(":")
    if name != "messages" or value != "enabled":
        return None if colon else (name, value, None)
    if not colon:
        return name, value, MESSAGES_MIN_THINKING_BUDGET
    if not budget.isascii() or not budget.isdigit() or budget.startswith("0"):
        return None
    if int(budget) < MESSAGES_MIN_THINKING_BUDGET:
        return None
    return name, value, int(budget)


# The effort each API carries (Chat `reasoning_effort`, Responses `reasoning.effort`, Messages
# `output_config.effort`), by the values its field takes. `effort_rendering` checks each one, and
# a request that leaves the effort out, against the marker the template renders.
EFFORT_VALUES = {
    "chat_completions": CHAT_REASONING_EFFORTS,
    "messages": MESSAGES_EFFORTS,
    "responses": RESPONSES_REASONING_EFFORTS,
}
# The `effort_rendering.efforts` key for a request that sends no effort at all.
UNSET_EFFORT = "unset"


@dataclass(frozen=True)
class EffortRendering:
    """The effort marker the model's template renders for each requested effort.

    A deployment maps requested efforts onto the ones its template knows (vLLM's
    `--reasoning-effort-map`) and no response reports the result, so the case reads the
    rendered prompt back through `prompt_logprobs` and finds the marker in it.
    """

    # A regular expression with exactly one capture group, matched against the rendered prompt.
    pattern: str
    # Requested effort, or `unset`, to the text the group must capture; None means the template
    # renders no marker at all for that effort.
    efforts: dict[str, str | None]
    surfaces: tuple[str, ...] = ("chat_completions",)


# What an unknown request element gets at an ingress: a field inside a conversation entry, a
# content block or part type, a tool type, a request header, an anthropic-beta value. Each is
# what a client release sends before any layer knows it. `unknown_item` is a Responses input
# item type; `count_tokens_block` is an unknown content block on Messages count_tokens;
# `unknown_enum_value` is a new value of the reasoning setting each API already has.
FORWARD_COMPAT_PROBES = (
    "unknown_message_field",
    "unknown_block",
    "unknown_tool_type",
    "unknown_header",
    "unknown_beta",
    "unknown_item",
    "count_tokens_block",
    "unknown_enum_value",
)
# `either` asserts only that the element is never answered with a 5xx or a dropped connection.
FORWARD_COMPAT_OUTCOMES = ("accepted", "rejected", "either")
# Byte-fidelity probes: what the client sent reaches the model unchanged through every hop.
BYTE_FIDELITY_PROBES = ("big_integer", "big_integer_history", "key_order", "unicode_escapes")
# Pinned renders: the input token count of a fixed conversation, with and without one tool.
PROMPT_PINS = ("conversation", "tools")
# Render pins share the prompt pin names and declare a SHA-256 digest of the rendered token ids.
RENDER_PIN_DIGEST_LENGTH = 64
# Contract baselines: declared key paths and JSON types of a response, a stream, an
# unknown-model error, a request-validation error, and a forced tool call answered whole and
# streamed.
CONTRACT_PARTS = ("response", "stream", "error", "invalid", "tool_response", "tool_stream")
CONTRACT_TYPES = ("string", "integer", "number", "boolean", "null", "object", "array", "absent")
# Optional Responses request features, each declared accepted, ignored or rejected under
# `capabilities.responses_features`. Wire shapes live in `surfaces/responses_features.py`.
RESPONSES_FEATURES = (
    "temperature",
    "top_p",
    "max_tool_calls",
    "parallel_tool_calls",
    "background_false",
    "user",
    "safety_identifier",
    "prompt_cache_key",
    "prompt_cache_retention",
    "prompt_cache_options",
    "service_tier",
    "truncation_auto",
    "text_format_text",
    "text_verbosity",
    "reasoning_summary",
    "reasoning_context",
    "encrypted_reasoning",
    "include_tool_outputs",
    "include_web_search_sources",
    "context_management",
    "moderation",
    "conversation",
    "prompt_template",
    "item_reference",
    "input_image",
    "input_file",
    "web_search",
    "web_search_preview",
    "file_search",
    "code_interpreter",
    "computer_use_preview",
    "mcp",
    "image_generation",
    "local_shell",
    "shell",
    "apply_patch",
    "reasoning_generate_summary",
    "reasoning_mode",
    "tool_search",
    "programmatic_tool_calling",
    "namespace_tools",
    "stream_obfuscation_off",
    "access_programs",
    "additional_tools",
)
# References to stored state: a fresh one can only be refused or ignored.
RESPONSES_REFERENCE_FEATURES = ("conversation", "prompt_template", "item_reference")
FEATURE_DISPOSITIONS = ("accepted", "ignored", "rejected")

# What an ingress does with a request field it is sent: generates normally with it (`accepted`),
# generates normally and the field has no effect (`ignored`), or refuses the request with a clean
# 4xx and an error envelope (`rejected`). A 5xx is never a disposition.
FIELD_DISPOSITIONS = ("accepted", "ignored", "rejected")
# Chat request fields with a declared-disposition probe. Each name is a field or the message shape
# the probe sends; the probe bodies live in `suites/openai_chat/fields.py`.
CHAT_FIELDS = (
    "role_system",
    "role_developer",
    "role_assistant",
    "assistant_refusal",
    "role_tool",
    "role_function",
    "message_name",
    "content_image_url",
    "content_file",
    "content_input_audio",
    "add_generation_prompt",
    "chat_template_kwargs",
    "chat_template",
    "documents",
    "truncation_side",
    "thinking_token_budget",
    "temperature",
    "top_p",
    "top_k",
    "min_p",
    "frequency_penalty",
    "presence_penalty",
    "repetition_penalty",
    "logit_bias",
    "length_penalty",
    "stop_token_ids",
    "seed",
    "bad_words",
    "use_beam_search",
    "top_logprobs_alone",
    "prompt_logprobs",
    "include_obfuscation",
    "continuous_usage_stats",
    "stream_options_alone",
    "stream_interval",
    "response_format_text",
    "include_reasoning",
    "user",
    "metadata",
    "store",
    "service_tier",
    "prediction",
    "modalities",
    "audio",
    "web_search_options",
    "safety_identifier",
    "prompt_cache_key",
    "prompt_cache_retention",
    "prompt_cache_options",
    "verbosity",
    "moderation",
    "parallel_tool_calls",
    "tools",
    "functions",
    "skip_special_tokens",
    "spaces_between_special_tokens",
    "add_special_tokens",
    "return_token_offsets",
    "return_prompt_text",
    "cache_salt",
    "request_id",
    "session_id",
    "structured_outputs",
    "guided_json",
    "guided_choice",
    "guided_regex",
    "guided_grammar",
    "guided_decoding_backend",
    "repetition_detection",
    "logprob_token_ids",
    "allowed_token_ids",
    "routed_experts_prompt_start",
    "system_only",
    "assistant_last",
    "consecutive_users",
    "empty_user_content",
    "system_content_parts",
    "assistant_content_parts",
)
# Completions request fields with a declared-disposition probe; bodies in
# `suites/openai_completions/fields.py`.
COMPLETIONS_FIELDS = (
    "suffix",
    "best_of",
    "temperature",
    "top_p",
    "frequency_penalty",
    "presence_penalty",
    "seed",
    "logit_bias",
    "user",
    "top_k",
    "min_p",
    "repetition_penalty",
    "min_tokens",
    "ignore_eos",
    "include_stop_str_in_output",
    "skip_special_tokens",
    "return_token_ids",
    "prompt_logprobs",
    "cache_salt",
    "response_format_json_object",
    "structured_outputs",
    "stream_options_alone",
    "use_beam_search",
    "length_penalty",
    "stop_token_ids",
    "spaces_between_special_tokens",
    "truncate_prompt_tokens",
    "truncation_side",
    "allowed_token_ids",
    "logprob_token_ids",
    "bad_words",
    "add_special_tokens",
    "request_id",
    "session_id",
    "return_tokens_as_token_ids",
    "routed_experts_prompt_start",
    "return_token_offsets",
    "repetition_detection",
    "thinking_token_budget",
    "stream_interval",
)
# Repeat behaviours of Chat sampling: the same seed returns the same text, greedy decoding repeats,
# and every request is a fresh generation.
CHAT_REPEATS = ("seed", "greedy", "fresh")


@dataclass(frozen=True)
class Capabilities:
    anthropic_betas: tuple[str, ...] = ()
    background_responses: bool = False
    cache_reuse: CacheReuse | None = None
    effort_rendering: EffortRendering | None = None
    # Declared disposition per Chat field probe in CHAT_FIELDS; an absent name skips undeclared.
    chat_fields: dict[str, str] = field(default_factory=dict)
    chat_options: tuple[str, ...] = ()
    chat_reasoning_efforts: tuple[str, ...] = ()
    chat_repeats: tuple[str, ...] = ()
    client_reuse: bool = False
    client_sessions: tuple[str, ...] = ()
    completions_fields: dict[str, str] = field(default_factory=dict)
    count_tokens: bool = False
    header_probe: bool = False
    header_rewrites: dict[str, str] = field(default_factory=dict)
    # The model reads images: a picture in a user turn or a tool result reaches it as a picture.
    vision: bool | tuple[str, ...] = False
    json_object: bool | tuple[str, ...] = False
    # An https URL of an image hosted online that the deployment can fetch, sent as the
    # Messages `url` image source; None leaves the image_url feature without its instrument.
    image_url_fixture: str | None = None
    messages_checks: tuple[str, ...] = ()
    messages_efforts: tuple[str, ...] = ()
    # Feature name -> "accepted" or "rejected"; see MESSAGES_FEATURES.
    messages_features: dict[str, str] = field(default_factory=dict)
    messages_thinking: tuple[str, ...] = ()
    # GET /v1/model/info: field -> the value the gateway must report for the model; empty is
    # undeclared.
    model_info: dict[str, Any] = field(default_factory=dict)
    # GET /v1/models/{id}: True serves the model, False refuses with a 4xx, None is undeclared.
    model_retrieval: bool | None = None
    # Spec-derived Chat and Completions response, stream and request-effect cases.
    openai_contract: bool | tuple[str, ...] = False
    prefix_stability: bool | tuple[str, ...] = False
    reasoning_tokens: bool | tuple[str, ...] = False
    responses_options: tuple[str, ...] = ()
    responses_reasoning_efforts: tuple[str, ...] = ()
    responses_features: dict[str, str] = field(default_factory=dict)
    # POST /v1/responses/input_tokens: True counts, False refuses with a 4xx, None is undeclared.
    responses_input_tokens: bool | None = None
    # POST /v1/responses/compact: True compacts, False refuses with a 4xx, None is undeclared.
    responses_compact: bool | None = None
    stateful_responses: bool = False
    # The fleet keeps no response state: retrieval, chaining and background generation of a
    # response it created are refused. Exclusive with stateful and background responses.
    stateless_responses: bool = False
    structured_output: bool | tuple[str, ...] = False
    structured_output_streaming: bool | tuple[str, ...] = False
    vllm_options: tuple[str, ...] = ()
    scenario_checks: tuple[str, ...] = ()
    # Model names clients send by default, such as a coding agent's sub-agent model. Each must be
    # served where it is one of the model's aliases and refused cleanly everywhere else.
    client_model_names: tuple[str, ...] = ()
    # Chat efforts the template answers without reasoning, sent top level or, where the ingress
    # declares the `chat_template_kwargs` option, as a template kwarg. None of them may be in
    # `chat_reasoning_efforts`, whose case expects reasoning below `none`.
    chat_quiet_efforts: tuple[str, ...] = ()
    forward_compat: dict[str, dict[str, str]] = field(default_factory=dict)
    byte_fidelity: tuple[str, ...] = ()
    prompt_pins: dict[str, dict[str, int]] = field(default_factory=dict)
    contract_baseline: dict[str, dict[str, dict[str, tuple[str, ...]]]] = field(
        default_factory=dict
    )
    render_pins: dict[str, dict[str, str]] = field(default_factory=dict)

    def supports_option(self, name: str) -> bool:
        return name in self.vllm_options


CLAUDE_CODE_MIN_VERSION = (2, 1, 0)


@dataclass(frozen=True)
class ClientBinary:
    version: str
    executable: Path


@dataclass(frozen=True)
class Client:
    """A real consumer of the fleet, and what it depends on.

    Client is a selector over cases rather than a fourth abstraction: `--client agent`
    compiles to a marker expression. It exists so the suite can answer the question the
    issue actually asks -- can this client work here -- instead of a question about
    endpoints.
    """

    id: str
    surfaces: tuple[str, ...] = ()
    needs: tuple[str, ...] = ()
    binaries: tuple[ClientBinary, ...] = ()


@dataclass(frozen=True)
class KnownFailure:
    """A failure the team has filed and traced to a fix the fleet does not carry yet.

    It labels a failure; it never excuses one. The case still runs, its verdict is still
    `fail`, and only a failure of the named check is covered. See `profile/known.py`.
    """

    # The first failing check, as verdict.txt and the dashboard's failure groups name it.
    # A glob with at least one literal character, so one entry can cover a family such as
    # `tool_call_arguments*` but never every check.
    check: str
    # Where it is tracked: `owner/repo#N`, `#N` (this repo) or a URL.
    issue: str
    # Globs over the case id, or `id[variant]`. Empty means any case failing the check.
    cases: tuple[str, ...] = ()
    # Ingress ids, from the profile's `ingress` key (one id or a list). Empty means any.
    ingresses: tuple[str, ...] = ()
    # Where the fix is, for the reader: a PR, a release, a config change.
    fix: str | None = None
    # The build the fix ships in, as signals the run records (`x-litellm-version`,
    # `system_fingerprint`, a router header). Responses at or past every one of them mean the
    # fix should have landed, and the failure is reported as a plain one again.
    until: dict[str, str] = field(default_factory=dict)

    @property
    def specificity(self) -> int:
        """How narrowly the entry is scoped: a case list and an ingress each count one."""
        return int(bool(self.cases)) + int(bool(self.ingresses))

    def names_check(self, name: str) -> bool:
        from .known import matches_case

        return matches_case((self.check,), name, None)

    def covers(self, case_id: str | None, variant: str | None, ingress: str | None) -> bool:
        from .known import matches_case

        if self.ingresses and ingress not in self.ingresses:
            return False
        return not self.cases or matches_case(self.cases, case_id, variant)


@dataclass(frozen=True)
class HeaderCase:
    """One (header, ingress, disposition) triple to verify.

    Carries `ingress_id` as a named field rather than a tuple position so the reporter can
    attribute the case to an ingress even when it skips before building a client -- otherwise an
    unverifiable disposition lands in no column at all and reads as "did not run".
    """

    header: str
    ingress_id: str
    disposition: Disposition


@dataclass(frozen=True)
class Profile:
    name: str
    model: Model
    ingresses: tuple[Ingress, ...]
    schema: str = "llmdv.profile/1"
    vantage: Vantage = Vantage.EXTERNAL
    environment: Environment = field(default_factory=Environment)
    capabilities: Capabilities = field(default_factory=Capabilities)
    headers: dict[str, dict[str, Disposition]] = field(default_factory=dict)
    clients: tuple[Client, ...] = ()
    # Declared, never inferred. See FleetPolicy.
    fleet_policy: FleetPolicy = FleetPolicy.DEDICATED
    source_path: Path | None = None
    # Configuration, metrics and log invariants. Empty unless the profile has a `fleet` block.
    fleet: FleetChecks = field(default_factory=FleetChecks)
    # Failures already filed and traced to a pending fix. Labels, never excuses.
    known_failures: tuple[KnownFailure, ...] = ()

    def ingress(self, ingress_id: str) -> Ingress:
        for item in self.ingresses:
            if item.id == ingress_id:
                return item
        raise KeyError(f"no ingress {ingress_id!r} in profile {self.name!r}")

    @property
    def ingress_ids(self) -> tuple[str, ...]:
        return tuple(item.id for item in self.ingresses)

    def chain(self) -> list[str]:
        """Declared ingress ids, outermost first.

        Ordered by what each `kind` fronts rather than by declaration order, so a profile that
        happens to list the engine first still reports the gateway as the outer layer. Two
        ingresses of the same kind tie and keep declaration order, `sorted` being stable.
        """
        return [item.id for item in sorted(self.ingresses, key=lambda i: _KIND_DEPTH[i.kind])]

    def client(self, client_id: str) -> Client:
        for item in self.clients:
            if item.id == client_id:
                return item
        raise KeyError(f"no client {client_id!r} in profile {self.name!r}")

    def disposition(self, header: str, ingress_id: str) -> Disposition | None:
        return self.headers.get(header.lower(), {}).get(ingress_id)

    def surfaces_for(self, ingress_id: str) -> tuple[str, ...]:
        return self.ingress(ingress_id).surfaces
