"""Synthetic paired fixtures; production representativeness is intentionally unclaimed."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class PrefixFixture:
    id: str
    surface: str
    left: dict
    right: dict
    controls: tuple[dict[str, Any], dict[str, Any]]


_STABLE = "Follow the user's instructions. " + "Stable reference word. " * 64
_USER = "Summarize the stable reference. "
_CONTROLS = ("Alpha control suffix.", "Zebra control suffix.")


def _messages(suffix: str) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": _STABLE},
        {"role": "user", "content": _USER + suffix},
    ]


FIXTURES = (
    PrefixFixture(
        "synthetic_chat_billing",
        "chat_render",
        {"messages": _messages("Billing marker: transient-aaa")},
        {"messages": _messages("Billing marker: transient-bbb")},
        tuple({"messages": _messages(suffix)} for suffix in _CONTROLS),
    ),
    PrefixFixture(
        "synthetic_messages_reminder",
        "messages_render",
        {
            "system": _STABLE,
            "messages": [
                {
                    "role": "user",
                    "content": _USER + "<system-reminder>transient-aaa</system-reminder>",
                }
            ],
        },
        {
            "system": _STABLE,
            "messages": [
                {
                    "role": "user",
                    "content": _USER + "<system-reminder>transient-bbb</system-reminder>",
                }
            ],
        },
        tuple(
            {"system": _STABLE, "messages": [{"role": "user", "content": _USER + suffix}]}
            for suffix in _CONTROLS
        ),
    ),
    PrefixFixture(
        "synthetic_responses_prefix",
        "responses_render",
        {"input": _messages("Billing marker: transient-aaa")},
        {"input": _messages("Billing marker: transient-bbb")},
        tuple({"input": _messages(suffix)} for suffix in _CONTROLS),
    ),
)
