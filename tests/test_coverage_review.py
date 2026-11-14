"""Regressions for the combined coverage review's false passes and false failures."""

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
import yaml

from llmdv.assertions import fail
from llmdv.assertions.logprobs import assert_logprobs
from llmdv.assertions.thinking import assert_thinking_blocks
from llmdv.clients.claude_code import isolated_env
from llmdv.clients.process import ProcessResult
from llmdv.evidence import journal
from llmdv.http.redact import install_secrets, reset_secrets
from llmdv.http.sse import read
from llmdv.profile.loader import ProfileError, load
from llmdv.profile.schema import Model
from llmdv.suites.conftest import check_completion
from llmdv.suites.contract.test_declarations import (
    test_declared_surfaces_exist as declared_surfaces_exist,
)
from llmdv.suites.contract.test_declarations import (
    test_undeclared_surfaces_are_really_absent as undeclared_surfaces_absent,
)
from llmdv.suites.real_clients.helpers import check_process
from llmdv.surfaces.base import Completion, SurfaceKind
from llmdv.surfaces.chat_completions import CHAT_COMPLETIONS
from llmdv.surfaces.messages import MESSAGES
from llmdv.surfaces.render import MESSAGES_RENDER
from llmdv.surfaces.responses import RESPONSES
from tests.fakes import MODEL, chat_tool_payload, chat_tool_stream


@pytest.fixture
def checks():
    active = journal.activate("review-regression")
    try:
        yield active.checks
    finally:
        journal.deactivate()


def test_stream_assembly_preserves_received_objects_and_tool_fragments(checks):
    body = CHAT_COMPLETIONS.tool_body(MODEL, {"properties": {"marker": {"enum": ["marker"]}}}, 64)
    payload = chat_tool_payload(body, MODEL, None)
    transcript = read(chat_tool_stream(payload, None).splitlines())
    frames = [e.json() for e in transcript.data_events]
    completion = CHAT_COMPLETIONS.assemble([CHAT_COMPLETIONS.parse_chunk(f) for f in frames])
    check_completion(completion, CHAT_COMPLETIONS, Model(MODEL))
    assert completion.raw == {"chunks": frames}
    assert completion.streamed and completion.object_type == "chat.completion.chunk"
    assert completion.has_output and not completion.text
    assert completion.assistant_message["tool_calls"][0]["id"] == completion.tool_calls[0].id
    assert next(c.observed for c in checks if c.name == "object") == "chat.completion.chunk"
    with pytest.raises(AssertionError, match="expected object"):
        check_completion(
            replace(completion, object_type="chat.completion"), CHAT_COMPLETIONS, Model(MODEL)
        )


def test_generic_completion_enforces_the_native_responses_contract():
    completion = Completion(
        None, "ok", "stop", object_type="response", model_echo=MODEL, raw={"output": "not an array"}
    )
    with pytest.raises(AssertionError, match="response_output"):
        check_completion(completion, RESPONSES, Model(MODEL))


def test_large_passing_payloads_have_compact_journals(checks):
    entry = {"token": "word", "logprob": -0.5, "top_logprobs": [{"token": "word", "logprob": -0.5}]}
    assert_logprobs([[entry] * 512, [entry] * 512], 2)
    completion = Completion(
        "assistant",
        "ok",
        "end_turn",
        raw={
            "content": [{"type": "thinking", "thinking": "x" * 250000, "signature": "s" * 250000}]
        },
    )
    assert_thinking_blocks(completion, "enabled")
    check_process(ProcessResult(0, "x" * 262144, "y" * 262144), "client_process")
    assert len(checks) == 3
    assert len(json.dumps([c.to_dict() for c in checks])) < 1500
    assert checks[0].observed["tokens"] == 1024


def test_bad_logprobs_after_a_valid_part_record_the_deciding_evidence(checks):
    entry = {"token": "ok", "logprob": -0.1, "top_logprobs": [{"token": "ok", "logprob": -0.1}]}
    with pytest.raises(AssertionError, match="output_logprobs"):
        assert_logprobs([[entry], []], 2)
    assert len(checks) == 1 and checks[0].ok is False


def test_failure_redacts_both_exception_and_journal(checks):
    install_secrets(["review-private-key"])
    try:
        with pytest.raises(AssertionError) as exc:
            fail("provider", {"error": "review-private-key"}, "review-private-key")
        assert "review-private-key" not in str(exc.value)
        assert "review-private-key" not in json.dumps([c.to_dict() for c in checks])
    finally:
        reset_secrets()


@pytest.mark.parametrize("status", [200, 201, 400, 401, 404, 405, 422, 500])
def test_only_successful_valid_requests_prove_a_route(status):
    client = SimpleNamespace(
        post=lambda *a, **k: SimpleNamespace(status=status, transport_error=None)
    )
    ingress = SimpleNamespace(
        id="router",
        surfaces=("messages_render",),
        rate_limited=False,
        exposes=lambda name: name != "responses_render",
    )
    if 200 <= status < 300:
        declared_surfaces_exist(client, Model(MODEL), ingress)
        with pytest.raises(AssertionError, match="profile omits"):
            undeclared_surfaces_absent(client, Model(MODEL), ingress)
    else:
        with pytest.raises(AssertionError, match="declares surfaces"):
            declared_surfaces_exist(client, Model(MODEL), ingress)
        undeclared_surfaces_absent(client, Model(MODEL), ingress)


def test_render_has_its_own_kind_and_openai_error_envelope():
    # vLLM scale_out/render/api_router.py serializes serve.engine.protocol.ErrorResponse
    # for Messages render as well as the other render handlers.
    surface = MESSAGES_RENDER
    assert surface.kind is SurfaceKind.RENDER
    assert MESSAGES.kind is SurfaceKind.GENERATION
    error = surface.parse_error(
        {"error": {"type": "NotFoundError", "message": "missing", "code": 404}}
    )
    assert error.message == "missing" and error.code == 404


def test_cli_model_aliases_are_explicit_and_old_versions_rejected(tmp_path):
    env = isolated_env(tmp_path, "http://127.0.0.1:1", MODEL)
    assert env["ANTHROPIC_SMALL_FAST_MODEL"] == env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == MODEL
    path = tmp_path / "old.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "clients": [
                    {"id": "cli", "binaries": [{"version": "2.0.59", "executable": "/tmp/claude"}]}
                ]
            }
        )
    )
    with pytest.raises(ProfileError, match="2.1.0"):
        load(path)


@pytest.mark.parametrize(
    "before,after,passes", [(401, 401, True), (200, 503, False), (401, 200, False)]
)
def test_disconnect_health_must_match_its_reachable_baseline(before, after, passes):
    from llmdv.suites.streaming.test_disconnect import _disconnect

    statuses = iter([before, after])
    client = SimpleNamespace(
        get=lambda path: SimpleNamespace(status=next(statuses), transport_error=None)
    )
    client.stream = lambda *a, **kw: (
        SimpleNamespace(
            status=200, transport_error=None, response_headers={"content-type": "text/event-stream"}
        ),
        SimpleNamespace(events=[], client_closed=True),
    )
    client.post = lambda *a, **kw: SimpleNamespace(
        status=200,
        transport_error=None,
        response_body={
            "object": "chat.completion",
            "model": MODEL,
            "choices": [
                {"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        },
    )
    ingress = SimpleNamespace(id="gateway", health_path="/health", rate_limited=False)
    if passes:
        _disconnect(client, CHAT_COMPLETIONS, Model(MODEL), ingress)
    else:
        with pytest.raises(AssertionError, match="health failed after disconnect"):
            _disconnect(client, CHAT_COMPLETIONS, Model(MODEL), ingress)
