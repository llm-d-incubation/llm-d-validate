"""A real client retains its conversation and native reuse across three bounded turns."""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from ...clients.claude_code import SESSION_OUTPUT_TOKENS, invoke, session_env
from ...clients.relay import ClientRelay
from ...evidence import journal
from ..cache.helpers import check_replicas, observation
from ..conftest import nonce_document
from .helpers import (
    check_client_result,
    check_native_generation,
    check_upstream,
    check_version,
    session_timeout,
)


@pytest.mark.surface("messages")
@pytest.mark.needs("client_reuse", "cache_reuse")
@pytest.mark.case_id("real_clients.claude_code_reuse")
def test_claude_code_multiturn_reuse(client, surface, model, ingress, client_binary):
    _, binary = client_binary
    config = ingress.capabilities.cache_reuse
    if "messages" not in config.surfaces:
        pytest.skip(f"[undeclared] ingress {ingress.id!r} declares no Messages cache reuse")
    if (
        os.name != "posix"
        or not binary.executable.is_file()
        or not os.access(binary.executable, os.X_OK)
    ):
        pytest.skip("[requires_instrumentation] install the declared executable on a POSIX runner")
    session = str(uuid.uuid4())
    marker = uuid.uuid4().hex
    first_prompt = (
        f"Remember this marker {marker}. {nonce_document(config.prefix_words)}\n\nReply ok."
    )
    observations = []
    # Three session turns, not a probe: a model that cannot turn thinking off spends a probe's
    # output budget thinking and never answers, so the turns get the sessions' budget.
    with (
        TemporaryDirectory(prefix="llmdv-client-") as temporary,
        ClientRelay(client, max_output_tokens=SESSION_OUTPUT_TOKENS) as relay,
    ):
        directory = Path(temporary)
        env = session_env(directory, relay.base_url, model.public_id)
        check_version(binary, directory, env)
        for turn in range(3):
            # Open one request slot per invocation: retries cannot consume a later turn's slot.
            relay.max_generations = turn + 1
            result = invoke(
                binary,
                directory=directory,
                env=env,
                prompt=first_prompt
                if turn == 0
                else "Keep the original reference and reply ok again.",
                timeout=session_timeout(ingress),
                session=session,
                resume=turn > 0,
            )
            # The relay's evidence and the fleet's status come before the client's; see
            # check_upstream. Only this turn's generations: earlier turns were checked already.
            ok = (
                len(relay.generations) == turn + 1
                and relay.attempts == turn + 1
                and not relay.rejected
            )
            journal.record(
                "client_turn_requests",
                {"turn": turn + 1, "attempts": relay.attempts, "rejected": relay.rejected},
                "exactly one new generation per turn",
                ok=ok,
            )
            check_upstream(relay, ingress, "client_upstream_statuses", since=turn)
            payload = check_client_result(result, model)
            same_session = payload.get("session_id") == session
            journal.record("client_session", payload.get("session_id"), session, ok=same_session)
            assert same_session, "client did not retain the requested conversation session"
            assert ok, "client retried or made an unexpected number of generations"
            generation = relay.generations[-1]
            check_native_generation(generation, surface, model, ingress)
            exchange = generation[0]
            history = exchange.request_body.get("messages")
            retained = marker in json.dumps(history)
            journal.record(
                "client_history_retained",
                {"turn": turn + 1, "retained": retained},
                True,
                ok=retained,
            )
            assert retained, "client lost the initial reference on a continuation turn"
            transcript = generation[1]
            usage = (
                surface.parse_cache_usage(exchange.response_body)
                if transcript is None
                else surface.stream_cache_usage(transcript)
            )
            observations.append(observation(f"turn {turn + 1}", usage, exchange, config))
    check_replicas(observations, config)
    ratios = [item.ratio for item in observations]
    ok = all(ratio >= config.min_ratio for ratio in ratios[1:])
    journal.record("client_turn_reuse", ratios, f"turns 2 and 3 >= {config.min_ratio}", ok=ok)
    assert ok, "native cache reuse collapsed on a continuation turn"
