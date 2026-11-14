"""Native cache counters: OpenAI reads are a subset; Messages counts are additive."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..http.sse import Transcript


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


@dataclass(frozen=True)
class CacheUsage:
    read_tokens: Any
    input_tokens: Any


def openai_cache(body: dict[str, Any], input_key: str, details_key: str) -> CacheUsage:
    usage = _mapping(body.get("usage"))
    return CacheUsage(_mapping(usage.get(details_key)).get("cached_tokens"), usage.get(input_key))


def messages_cache(body: dict[str, Any]) -> CacheUsage:
    usage = _mapping(body.get("usage"))
    counts = [
        usage.get("input_tokens"),
        usage.get("cache_read_input_tokens"),
        usage.get("cache_creation_input_tokens", 0),
    ]
    total = sum(counts) if all(type(value) is int and value >= 0 for value in counts) else None
    return CacheUsage(counts[1], total)


def stream_frames(transcript: Transcript) -> list[dict[str, Any]]:
    """The JSON objects a stream carried, for a surface to find its cache counters in."""
    frames = (event.json() for event in transcript.data_events)
    return [body for body in frames if isinstance(body, dict)]
