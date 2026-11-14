"""Named Messages request features, each built as a client sends it and judged by its effect.

A feature is a field, header or content shape from the Messages API that a fleet either serves
or refuses. `check` says what serving it means:

- `answer`: the request carries a random marker only the feature's content holds, so the reply
  can only contain it if that content reached the model;
- `valid`: an ordinary well-formed Message is enough, because the field changes nothing a
  client can observe on a self-hosted fleet;
- `prefill`, `prewarm`, `window`: the specific results the API defines for those requests.
"""

from __future__ import annotations

import base64
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ...profile.schema import MESSAGES_FEATURES, Model
from ...surfaces.base import TOOL_NAME, Surface
from ..conftest import budget_for

QUESTION = "Reply with only the code word you were given, or NONE."
OK_PROMPT = "Reply with the single word ok."
PREFILL = '{"code": "'
# A 1x1 PNG.
PNG = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAAB"
    "JRU5ErkJggg=="
)


def new_marker() -> str:
    return f"llmdv{uuid.uuid4().hex[:10]}"


def pdf_with_text(text: str) -> bytes:
    """A one-page PDF whose content stream holds `text` uncompressed."""
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref,
    )
    return bytes(out)


@dataclass(frozen=True)
class Request:
    body: dict[str, Any]
    headers: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Feature:
    check: str
    build: Callable[[Surface, Model, str], Request]
    # Whether count_tokens takes the same input; URL sources and hosted tools it does not.
    counted: bool = False
    # The text the request asks with. Counted alone it is the count_tokens baseline, so the
    # difference is the feature's content and not a second prompt.
    prompt: str = QUESTION
    # A capability naming an instrument the request needs, such as a hosted image. `build`
    # then receives its declared value in place of the marker, as its check reads no marker.
    fixture: str | None = None


# What `build` receives in place of a fixture when the feature is declared rejected. A refusal
# needs no instrument the deployment can reach, so any URL proves it: this one is on a reserved
# `.invalid` host (RFC 2606), which no deployment can fetch.
UNREACHABLE_FIXTURE = "https://images.example.invalid/llmdv.png"


def _plain(**extra: Any) -> Callable[[Surface, Model, str], Request]:
    def build(surface: Surface, model: Model, marker: str) -> Request:
        body = surface.minimal_body(model.public_id, budget_for(model), OK_PROMPT)
        body.pop("temperature")
        body.update(extra)
        return Request(body)

    return build


def _with_beta(beta: str, **extra: Any) -> Callable[[Surface, Model, str], Request]:
    plain = _plain(**extra)

    def build(surface: Surface, model: Model, marker: str) -> Request:
        return Request(plain(surface, model, marker).body, {"anthropic-beta": beta})

    return build


def _turns(make: Callable[[str], list[dict[str, Any]]], **extra: Any):
    def build(surface: Surface, model: Model, marker: str) -> Request:
        body = surface.minimal_body(model.public_id, budget_for(model, floor=256), QUESTION)
        body["messages"] = make(marker)
        body.update(extra)
        return Request(body)

    return build


def _user_blocks(*blocks: Callable[[str], dict[str, Any]], prompt: str = QUESTION):
    return _turns(
        lambda marker: [
            {
                "role": "user",
                "content": [*(b(marker) for b in blocks), {"type": "text", "text": prompt}],
            }
        ]
    )


def _note(marker: str) -> str:
    return f"The code word is {marker}."


def _document(source: Callable[[str], dict[str, Any]], **extra: Any):
    return lambda marker: {"type": "document", "source": source(marker), "title": "notes", **extra}


def _text_source(marker: str) -> dict[str, Any]:
    return {"type": "text", "media_type": "text/plain", "data": _note(marker)}


def _pdf_source(marker: str) -> dict[str, Any]:
    data = base64.b64encode(pdf_with_text(_note(marker))).decode()
    return {"type": "base64", "media_type": "application/pdf", "data": data}


def _image(source: dict[str, Any]):
    return lambda marker: {"type": "image", "source": source}


def _system_blocks(count: int, ttl: str | None = None):
    def build(surface: Surface, model: Model, marker: str) -> Request:
        control: dict[str, Any] = {"type": "ephemeral", **({"ttl": ttl} if ttl else {})}
        system = [
            {"type": "text", "text": f"Rule {index}: be brief.", "cache_control": control}
            for index in range(count)
        ]
        return _plain(system=system)(surface, model, marker)

    return build


def _assistant_said(
    *blocks: Callable[[str], dict[str, Any]], opening: str, marker_text: bool = True
):
    def content(marker: str) -> list[dict[str, Any]]:
        result = [b(marker) for b in blocks]
        if marker_text:
            result.append({"type": "text", "text": _note(marker)})
        return result

    return _turns(
        lambda marker: [
            {"role": "user", "content": opening},
            {
                "role": "assistant",
                "content": content(marker),
            },
            {"role": "user", "content": QUESTION},
        ]
    )


def _tool_turns(result: Callable[[str], list[dict[str, Any]]], prompt: str = QUESTION):
    def build(surface: Surface, model: Model, marker: str) -> Request:
        request = _turns(
            lambda m: [
                {"role": "user", "content": "Look up the code word."},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "id": "toolu_llmdv1", "name": TOOL_NAME, "input": {}}
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_llmdv1",
                            "content": result(m),
                        },
                        {"type": "text", "text": prompt},
                    ],
                },
            ]
        )(surface, model, marker)
        request.body["tools"] = [
            {
                "name": TOOL_NAME,
                "description": "Return the code word.",
                "input_schema": {"type": "object", "properties": {}},
            }
        ]
        return request

    return build


def _cached_message(surface: Surface, model: Model, marker: str) -> Request:
    body = _plain()(surface, model, marker).body
    body["messages"][-1]["content"] = [
        {"type": "text", "text": OK_PROMPT, "cache_control": {"type": "ephemeral"}}
    ]
    return Request(body)


def _cached_tool(surface: Surface, model: Model, marker: str) -> Request:
    tool = {
        "name": TOOL_NAME,
        "description": "Return a value.",
        "input_schema": {"type": "object", "properties": {}},
        "cache_control": {"type": "ephemeral"},
    }
    return _plain(tools=[tool])(surface, model, marker)


def _window(surface: Surface, model: Model, marker: str) -> Request:
    body = surface.minimal_body(model.public_id, model.max_model_len or 0, OK_PROMPT)
    body.pop("temperature")
    return Request(body, {"anthropic-beta": "model-context-window-exceeded-2025-08-26"})


def _prefill(surface: Surface, model: Model, marker: str) -> Request:
    return _turns(
        lambda m: [
            {
                "role": "user",
                "content": f"{_note(m)} Give a JSON object with one key, code, whose value is "
                "the code word.",
            },
            {"role": "assistant", "content": PREFILL},
        ]
    )(surface, model, marker)


FEATURES: dict[str, Feature] = {
    "metadata_user_id": Feature("valid", _plain(metadata={"user_id": "llmdv-probe-user"})),
    "top_p": Feature("valid", _plain(top_p=0.9)),
    "top_k": Feature("valid", _plain(top_k=10)),
    "service_tier": Feature("valid", _plain(service_tier="auto")),
    "container": Feature("valid", _plain(container="llmdv-container")),
    "mcp_servers": Feature(
        "valid",
        _with_beta(
            "mcp-client-2025-11-20",
            mcp_servers=[
                {"type": "url", "url": "https://mcp.example.invalid/sse", "name": "llmdv"}
            ],
        ),
    ),
    "inference_geo": Feature("valid", _plain(inference_geo="us")),
    "diagnostics": Feature("valid", _plain(diagnostics={"previous_message_id": None})),
    "speed_fast": Feature("valid", _with_beta("fast-mode-2026-02-01", speed="fast")),
    "top_level_cache_control": Feature("valid", _plain(cache_control={"type": "ephemeral"})),
    "cache_control_ttl_1h": Feature("valid", _system_blocks(1, ttl="1h")),
    "cache_breakpoints_five": Feature("valid", _system_blocks(5)),
    "cache_control_message_block": Feature("valid", _cached_message),
    "cache_control_tool": Feature("valid", _cached_tool),
    "stop_sequences_five": Feature(
        "valid", _plain(stop_sequences=[f"llmdv-stop-{index}" for index in range(5)])
    ),
    "max_tokens_zero": Feature("prewarm", _plain(max_tokens=0)),
    # The output cap coding agents are told to set.
    "max_tokens_large": Feature("valid", _plain(max_tokens=64000)),
    "max_tokens_over_window": Feature("window", _window),
    "thinking_display_omitted": Feature(
        "valid", _plain(thinking={"type": "adaptive", "display": "omitted"})
    ),
    "thinking_display_updates": Feature(
        "valid",
        _with_beta(
            "thinking-display-updates-2026-08-18",
            thinking={"type": "adaptive", "display": "updates"},
        ),
    ),
    "thinking_between_tools": Feature("valid", _plain(thinking={"type": "between_tools"})),
    "consecutive_user_turns": Feature(
        "answer",
        _turns(
            lambda m: [{"role": "user", "content": _note(m)}, {"role": "user", "content": QUESTION}]
        ),
        counted=True,
    ),
    "consecutive_assistant_turns": Feature(
        "answer",
        _turns(
            lambda m: [
                {"role": "user", "content": "Remember a code word for me."},
                {"role": "assistant", "content": "Noted."},
                {"role": "assistant", "content": _note(m)},
                {"role": "user", "content": QUESTION},
            ]
        ),
        counted=True,
    ),
    "leading_assistant": Feature(
        "answer",
        _turns(
            lambda m: [
                {"role": "assistant", "content": _note(m)},
                {"role": "user", "content": QUESTION},
            ]
        ),
        counted=True,
    ),
    "assistant_prefill": Feature("prefill", _prefill),
    "document_text": Feature("answer", _user_blocks(_document(_text_source)), counted=True),
    "document_pdf": Feature("answer", _user_blocks(_document(_pdf_source)), counted=True),
    "document_content": Feature(
        "answer",
        _user_blocks(
            _document(
                lambda m: {"type": "content", "content": [{"type": "text", "text": _note(m)}]}
            )
        ),
        counted=True,
    ),
    "document_citations": Feature(
        "answer", _user_blocks(_document(_text_source, citations={"enabled": True}))
    ),
    "search_result": Feature(
        "answer",
        _user_blocks(
            lambda m: {
                "type": "search_result",
                "source": "https://docs.example.invalid/notes",
                "title": "Project notes",
                "content": [{"type": "text", "text": _note(m)}],
            }
        ),
        counted=True,
    ),
    "image_base64": Feature(
        "valid",
        _user_blocks(
            _image({"type": "base64", "media_type": "image/png", "data": PNG}), prompt=OK_PROMPT
        ),
        counted=True,
        prompt=OK_PROMPT,
    ),
    # The `url` image source, as Anthropic documents it: an image hosted online, fetched by the
    # deployment. The URL is the profile's `image_url_fixture`, one the deployment can reach.
    "image_url": Feature(
        "valid",
        _user_blocks(
            lambda url: {"type": "image", "source": {"type": "url", "url": url}},
            prompt=OK_PROMPT,
        ),
        fixture="image_url_fixture",
    ),
    "tool_result_blocks": Feature(
        "answer", _tool_turns(lambda m: [{"type": "text", "text": _note(m)}]), counted=True
    ),
    "tool_result_image": Feature(
        "valid",
        _tool_turns(
            lambda m: [
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/png", "data": PNG},
                }
            ],
            prompt=OK_PROMPT,
        ),
    ),
    "redacted_thinking_passback": Feature(
        "answer",
        _assistant_said(
            lambda m: {"type": "redacted_thinking", "data": "llmdv-opaque-thinking"},
            opening="Remember a code word for me.",
        ),
        counted=True,
    ),
    "server_tool_history": Feature(
        "answer",
        _assistant_said(
            lambda m: {
                "type": "server_tool_use",
                "id": "srvtoolu_llmdv1",
                "name": "web_search",
                "input": {"query": _note(m)},
            },
            lambda m: {
                "type": "web_search_tool_result",
                "tool_use_id": "srvtoolu_llmdv1",
                "content": [],
            },
            opening="Search for the code word.",
            marker_text=False,
        ),
    ),
    "text_citations_passback": Feature(
        "answer",
        _turns(
            lambda m: [
                {"role": "user", "content": "What is the code word in the notes?"},
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "text",
                            "text": _note(m),
                            "citations": [
                                {
                                    "type": "char_location",
                                    "cited_text": _note(m),
                                    "document_index": 0,
                                    "document_title": "notes",
                                    "start_char_index": 0,
                                    "end_char_index": len(_note(m)),
                                }
                            ],
                        }
                    ],
                },
                {"role": "user", "content": QUESTION},
            ]
        ),
    ),
}

if set(FEATURES) != set(MESSAGES_FEATURES):  # pragma: no cover - a llmdv defect, caught at import
    raise RuntimeError("FEATURES and MESSAGES_FEATURES name different features")

COUNTED = tuple(name for name in MESSAGES_FEATURES if FEATURES[name].counted)
# Fields only a generation reads; count_tokens takes the input alone.
GENERATION_ONLY = ("max_tokens", "temperature", "top_p", "top_k", "stop_sequences", "stream")


def count_body(request: Request) -> dict[str, Any]:
    return {k: v for k, v in request.body.items() if k not in GENERATION_ONLY}
