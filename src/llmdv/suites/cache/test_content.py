"""Reuse of prompts whose content is hard to tokenize: many scripts, and one very long line.

A prefix-aware router tokenizes the prompt itself to find the replica that holds it. Where its
tokenization disagrees with the engine's, or it cannot tokenize the prompt in time, a repeat is
placed blind and recomputes. Each case sends one such prompt twice; the first must be answered
and the repeat must read the whole-block floor of the first.
"""

from __future__ import annotations

import json
import random

import pytest

from ...evidence import journal
from ...profile.schema import CacheReuse
from ..conftest import nonce_document
from .helpers import cache_config, check_cold, check_replicas, check_warm, observe

# Words from scripts a byte-level tokenizer splits into many tokens. The source stays ASCII.
SCRIPT_WORDS = (
    # Latin with decomposed combining marks, which a normalizing tokenizer composes.
    "cafe\u0301",
    "man\u0303ana",
    "a\u030angstro\u0308m",
    # Greek and Cyrillic.
    "\u03ba\u03cc\u03c3\u03bc\u03b5",
    "\u043f\u0440\u0438\u0432\u0435\u0442",
    # Hebrew and Arabic, written right to left.
    "\u05e9\u05dc\u05d5\u05dd",
    "\u0645\u0631\u062d\u0628\u0627",
    # Devanagari with a conjunct, and Thai.
    "\u0928\u092e\u0938\u094d\u0924\u0947",
    "\u0e2a\u0e27\u0e31\u0e2a\u0e14\u0e35",
    # Chinese, Japanese kana and Korean.
    "\u4f60\u597d\u4e16\u754c",
    "\u3053\u3093\u306b\u3061\u306f",
    "\ud55c\uad6d\uc5b4",
    # Emoji: a zero-width-joiner family, a skin tone modifier, a flag pair and a keycap.
    "\U0001f468\u200d\U0001f469\u200d\U0001f467",
    "\U0001f44b\U0001f3fd",
    "\U0001f1fa\U0001f1f3",
    "1\ufe0f\u20e3",
)


def multilingual(words: int) -> str:
    """The reference document with a word from another script after each of its words."""
    head, _, text = nonce_document(max(words // 2, 1)).partition("\n\n")
    rng = random.Random(head)
    mixed = [w for word in text.split() for w in (word, rng.choice(SCRIPT_WORDS))]
    lines = (" ".join(mixed[start : start + 60]) for start in range(0, len(mixed), 60))
    return f"{head}\n\n" + "\n\n".join(lines)


def long_line(words: int) -> str:
    """The reference document as one line: a JSON array, as a minified data file is."""
    head, _, text = nonce_document(words).partition("\n\n")
    return f"{head}\n\n{json.dumps(text.split())}"


CONTENT = {"multilingual": multilingual, "long_line": long_line}


def content_prompt(kind: str, config: CacheReuse) -> str:
    document = CONTENT[kind](config.prefix_words)
    journal.record(
        "content_shape",
        {
            "kind": kind,
            "chars": len(document),
            "longest_line_chars": max(len(line) for line in document.splitlines()),
            "non_ascii_chars": sum(not c.isascii() for c in document),
        },
    )
    return f"{document}\n\nReply with the word ok."


@pytest.mark.surface_generic("cache_usage")
@pytest.mark.needs("cache_reuse")
@pytest.mark.case_id("cache.content_reuse")
@pytest.mark.parametrize("kind", list(CONTENT))
def test_content_reuse(client, surface, model, ingress, kind):
    """A prompt in many scripts, or holding one very long line, is answered and reused."""
    config = cache_config(ingress, surface)
    body = surface.minimal_body(model.public_id, max_tokens=16, prompt=content_prompt(kind, config))
    first, repeat = [
        observe(client, surface, model, ingress, body, label) for label in ("first", "repeat")
    ]
    check_replicas([first, repeat], config)
    check_cold(first, config)
    check_warm(first, repeat, config)
