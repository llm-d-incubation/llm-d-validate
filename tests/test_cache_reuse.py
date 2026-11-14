import json

import pytest
import yaml

from llmdv.http.sse import read
from llmdv.profile.loader import ProfileError, load
from llmdv.surfaces.cache import messages_cache
from llmdv.surfaces.chat_completions import CHAT_COMPLETIONS
from llmdv.surfaces.messages import MESSAGES
from llmdv.surfaces.responses import RESPONSES
from tests.fakes import MODEL, FakeFleet, message_events, named_sse, response_body, response_events
from tests.test_suites_against_fake import run_suites, write_profile

RATIO = {"min_ratio": 0.8, "replica_header": "x-llmdv-replica"}
BLOCKS = {"min_ratio": 0.8, "affinity": "routed", "block_size": 16}


def run_cache_case(
    tmp_path,
    surface,
    declaration,
    select,
    defect=None,
    model=None,
    nights=1,
    fake=None,
    **capabilities,
):
    """Run one cache case on one surface of a fake fleet with a prefix block cache.

    With `nights` above 1 the case runs again on the same fleet, whose cache keeps what the
    earlier runs left, and the last run's result is returned. `fake` passes further settings to
    the fake fleet.
    """
    block = declaration.get("block_size") or 16
    with FakeFleet(
        cache_probe=True,
        cache_defect=defect,
        cache_block=block,
        max_model_len=4096,
        # The fake refuses images, as a text-only model does, unless told it reads them.
        transport={"multimodal": True} if capabilities.get("vision") else None,
        **(fake or {}),
    ) as fleet:
        path = write_profile(tmp_path, fleet, responses=True, max_model_len=4096)
        raw = yaml.safe_load(path.read_text())
        raw["model"].update(model or {})
        raw["ingresses"][0]["surfaces"] = [surface]
        cache = {"surfaces": [surface], "prefix_words": 128, **declaration}
        raw["capabilities"] = {"cache_reuse": cache, **capabilities}
        path.write_text(yaml.safe_dump(raw))
        for night in range(nights):
            run = tmp_path / ("run" if night == 0 else f"run-{night}")
            result = run_suites(path, run, "-k", select, "--llmdv-surface", surface)
        return result


@pytest.mark.parametrize("surface", ["chat_completions", "messages", "responses"])
@pytest.mark.parametrize(
    "declaration,defect,outcome",
    [
        (RATIO, None, "pass"),
        (RATIO, "cold", "fail"),
        (RATIO, "missing", "fail"),
        (RATIO, "impossible", "fail"),
        (RATIO, "replica", "skip"),
        # A ratio cannot see one block missing, or a first request that was never cold.
        (RATIO, "short_block", "pass"),
        (RATIO, "stale_first", "pass"),
        (BLOCKS, None, "pass"),
        (BLOCKS, "short_block", "fail"),
        (BLOCKS, "stale_first", "fail"),
        # Routed: a repeat placed on a replica without the prefix is the failure.
        (BLOCKS, "replica", "fail"),
    ],
)
def test_native_reuse_contract(tmp_path, surface, declaration, defect, outcome):
    code, records = run_cache_case(
        tmp_path, surface, declaration, "test_native_cache_reuse", defect
    )
    assert code == (1 if outcome == "fail" else 0), records
    assert records[0]["outcome"] == outcome, records
    if declaration is BLOCKS and defect in ("short_block", "replica"):
        assert "floor" in records[0]["reason"], records
    if declaration is BLOCKS and defect == "stale_first":
        assert "expected none" in records[0]["reason"], records


@pytest.mark.parametrize(
    "override",
    [
        {"min_ratio": 0},
        {"min_ratio": "0.8"},
        {"min_ratio": True},
        {"min_ratio": float("nan")},
        {"prefix_words": True},
        {"single_replica": "true"},
        {"replica_header": "authorization"},
        {"unknown": 1},
        {"block_size": 0},
        {"block_size": True},
        {"block_size": "16"},
        {"block_size": 2048},
        {"affinity": "sticky"},
        {"affinity": "routed"},
        {"canonical_tool_keys": "yes"},
    ],
)
def test_cache_contract_is_strict(tmp_path, override):
    path = cache_profile(tmp_path, {"min_ratio": 0.8, "single_replica": True, **override})
    with pytest.raises(ProfileError, match="cache_reuse"):
        load(path)


def cache_profile(tmp_path, cache_reuse, engine_override=None):
    with FakeFleet() as fleet:
        path = write_profile(tmp_path, fleet)
    raw = yaml.safe_load(path.read_text())
    raw["capabilities"] = {"cache_reuse": cache_reuse}
    if engine_override is not None:
        raw["ingresses"][0]["capabilities"] = {"cache_reuse": engine_override}
    path.write_text(yaml.safe_dump(raw))
    return path


def test_cache_contract_reports_every_problem_at_once(tmp_path):
    path = cache_profile(
        tmp_path, {"min_ratio": 0, "block_size": -1, "affinity": "sticky", "single_replica": True}
    )
    with pytest.raises(ProfileError) as error:
        load(path)
    for field in ("min_ratio", "block_size", "affinity", "declare exactly one"):
        assert field in str(error.value)


def test_existing_declarations_keep_ratio_rules(tmp_path):
    config = load(cache_profile(tmp_path, {"min_ratio": 0.8, "single_replica": True}))
    cache = config.ingresses[0].capabilities.cache_reuse
    assert (cache.block_size, cache.affinity) == (None, None)


def test_routed_affinity_and_block_size_are_overridable(tmp_path):
    routed = {"min_ratio": 0.8, "prefix_words": 128, "affinity": "routed", "block_size": 16}
    single = {"affinity": None, "block_size": None, "single_replica": True}
    config = load(cache_profile(tmp_path, routed, engine_override=single))
    inherited = config.capabilities.cache_reuse
    engine = config.ingresses[0].capabilities.cache_reuse
    assert (inherited.affinity, inherited.block_size, inherited.single_replica) == (
        "routed",
        16,
        False,
    )
    assert (engine.affinity, engine.block_size, engine.single_replica) == (None, None, True)


def test_messages_denominator_includes_read_and_creation():
    usage = messages_cache(
        {
            "usage": {
                "input_tokens": 10,
                "cache_read_input_tokens": 70,
                "cache_creation_input_tokens": 20,
            }
        }
    )
    assert usage.input_tokens == 100 and usage.read_tokens == 70


def _transcript(stream):
    return read(stream.splitlines())


def test_chat_stream_cache_reads_the_terminal_usage_frame():
    def frame(choices, usage=None):
        body = {"object": "chat.completion.chunk", "model": MODEL, "choices": choices}
        return {**body, "usage": usage} if usage else body

    cached = {"prompt_tokens": 100, "prompt_tokens_details": {"cached_tokens": 96}}
    stream = "".join(
        f"data: {json.dumps(body)}\n\n"
        for body in (
            frame([{"index": 0, "delta": {"content": "ok"}}], usage={"prompt_tokens": 100}),
            frame([{"index": 0, "delta": {}, "finish_reason": "stop"}]),
            frame([], usage=cached),
        )
    )
    usage = CHAT_COMPLETIONS.stream_cache_usage(_transcript(stream + "data: [DONE]\n\n"))
    assert (usage.read_tokens, usage.input_tokens) == (96, 100)


def test_messages_stream_cache_reads_message_start_not_the_final_delta():
    events = message_events()
    events[0]["message"]["usage"] = {
        "input_tokens": 4,
        "cache_read_input_tokens": 96,
        "cache_creation_input_tokens": 0,
    }
    events[-2]["usage"] = {"output_tokens": 2, "cache_read_input_tokens": 0}
    usage = MESSAGES.stream_cache_usage(_transcript(named_sse(events)))
    assert (usage.read_tokens, usage.input_tokens) == (96, 100)


def _start_without_cache(events):
    events[0]["message"]["usage"] = {"input_tokens": 100, "output_tokens": 0}
    return events


def test_messages_stream_cache_reads_the_final_delta_when_message_start_omits_it():
    events = _start_without_cache(message_events())
    events[-2]["usage"] = {
        "output_tokens": 2,
        "input_tokens": 4,
        "cache_read_input_tokens": 80,
        "cache_creation_input_tokens": 16,
    }
    usage = MESSAGES.stream_cache_usage(_transcript(named_sse(events)))
    assert (usage.read_tokens, usage.input_tokens) == (80, 100)


def test_messages_stream_cache_takes_the_last_delta_carrying_the_counts():
    events = _start_without_cache(message_events())
    early = {"type": "message_delta", "delta": {}, "usage": {"output_tokens": 1}}
    early["usage"].update(input_tokens=100, cache_read_input_tokens=0)
    events.insert(-2, early)
    events[-2]["usage"] = {"output_tokens": 2, "input_tokens": 4, "cache_read_input_tokens": 96}
    usage = MESSAGES.stream_cache_usage(_transcript(named_sse(events)))
    assert (usage.read_tokens, usage.input_tokens) == (96, 100)


def test_messages_stream_cache_never_mixes_start_and_delta_counts():
    """A delta carrying the read without its input count leaves the total unknown."""
    events = _start_without_cache(message_events())
    events[-2]["usage"] = {"output_tokens": 2, "cache_read_input_tokens": 96}
    usage = MESSAGES.stream_cache_usage(_transcript(named_sse(events)))
    assert (usage.read_tokens, usage.input_tokens) == (96, None)


def test_responses_stream_cache_reads_the_terminal_snapshot():
    final = response_body()
    final["usage"] = {
        "input_tokens": 100,
        "output_tokens": 2,
        "total_tokens": 102,
        "input_tokens_details": {"cached_tokens": 96},
    }
    usage = RESPONSES.stream_cache_usage(_transcript(named_sse(response_events(response=final))))
    assert (usage.read_tokens, usage.input_tokens) == (96, 100)


@pytest.mark.parametrize("adapter", [CHAT_COMPLETIONS, MESSAGES, RESPONSES])
def test_a_stream_without_cache_counters_reads_as_missing(adapter):
    usage = adapter.stream_cache_usage(_transcript("data: [DONE]\n\n"))
    assert usage.read_tokens is None


@pytest.mark.parametrize("surface", ["chat_completions", "messages", "responses"])
@pytest.mark.parametrize(
    "declaration,defect,outcome,check",
    [
        (BLOCKS, None, "pass", None),
        (BLOCKS, "image_blind", "fail", "cache_follows_image"),
        (RATIO, None, "pass", None),
        (RATIO, "image_blind", "fail", "cache_follows_image"),
    ],
)
def test_cache_reuse_follows_the_image(tmp_path, surface, declaration, defect, outcome, check):
    code, records = run_cache_case(
        tmp_path,
        surface,
        declaration,
        "test_cache_reuse_follows_the_image",
        defect,
        vision=True,
    )
    row = next(r for r in records if r.get("surface") == surface)
    assert row["outcome"] == outcome, row
    assert code == (0 if outcome == "pass" else 1)
    if check:
        assert check in [c["name"] for c in row["checks"] if c.get("ok") is False]


def test_the_image_cache_case_needs_images_declared(tmp_path):
    _, records = run_cache_case(tmp_path, "messages", BLOCKS, "test_cache_reuse_follows_the_image")
    row = next(r for r in records if r.get("surface") == "messages")
    assert row["skip_class"] == "undeclared"
