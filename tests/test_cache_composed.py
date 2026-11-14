"""The composed agent session, proven to fail on each defect it targets."""

import pytest
import yaml

from tests.fakes import ATTRIBUTION_PREFIX, FakeFleet
from tests.test_cache_repeats import TOOLS
from tests.test_cache_reuse import BLOCKS, RATIO
from tests.test_suites_against_fake import run_suites, write_profile

AGENT = {
    "messages_thinking": ["enabled", "adaptive"],
    "messages_efforts": ["low", "high"],
    "anthropic_betas": ["interleaved-thinking-2025-05-14"],
    "scenario_checks": ["inline_system"],
}
ATTRIBUTED = {**BLOCKS, "stripped_system_prefix": ATTRIBUTION_PREFIX}


def run_composed(tmp_path, declaration, defect=None, capabilities=AGENT, **fleet_options):
    block = declaration.get("block_size") or 16
    with FakeFleet(
        cache_probe=True,
        cache_defect=defect,
        cache_block=block,
        max_model_len=4096,
        **fleet_options,
    ) as fleet:
        path = write_profile(tmp_path, fleet, responses=True, max_model_len=4096)
        raw = yaml.safe_load(path.read_text())
        raw["model"].update(TOOLS)
        raw["ingresses"][0]["surfaces"] = ["messages"]
        cache = {"surfaces": ["messages"], "prefix_words": 128, **declaration}
        raw["capabilities"] = {"cache_reuse": cache, **capabilities}
        path.write_text(yaml.safe_dump(raw))
        code, records = run_suites(
            path, tmp_path / "run", "-k", "test_composed_session", "--llmdv-surface", "messages"
        )
        return code, records, list(fleet.seen)


@pytest.mark.parametrize(
    "declaration,defect,options,outcome",
    [
        (BLOCKS, None, {}, "pass"),
        (RATIO, None, {}, "pass"),
        (ATTRIBUTED, None, {}, "pass"),
        # Streamed counters only in the final delta, as the gateway reports them (#333).
        (BLOCKS, "stream_cache_in_delta", {}, "pass"),
        # Each part passes alone; these stacks fail only the composed request.
        (BLOCKS, None, {"refuse_query_string": True}, "fail"),
        (BLOCKS, None, {"refuse_thinking_tools": True}, "fail"),
        # Served, but the session loses its reuse.
        (BLOCKS, "history_blind", {}, "fail"),
        (ATTRIBUTED, "attribution_kept", {}, "fail"),
    ],
)
def test_composed_session_fails_on_each_defect(tmp_path, declaration, defect, options, outcome):
    code, records, _ = run_composed(tmp_path, declaration, defect, **options)
    assert code == (1 if outcome == "fail" else 0), records
    assert [r["outcome"] for r in records] == [outcome], records


def test_composed_session_sends_every_declared_part(tmp_path):
    code, records, seen = run_composed(tmp_path, ATTRIBUTED)
    assert code == 0, records
    parts = next(c["observed"] for c in records[0]["checks"] if c["name"] == "composed_parts")
    assert parts == {
        "query": "beta=true",
        "beta_header": True,
        "attribution_line": True,
        "tools": 2,
        "thinking": "adaptive",
        "effort": "high",
        "max_tokens": 2048,
        "inline_system": True,
    }, parts
    posts = [(path, headers) for method, path, headers in seen if method == "POST"]
    assert len(posts) == 2, posts
    assert all(h.get("anthropic-beta") == "interleaved-thinking-2025-05-14" for _, h in posts)


def test_composed_session_sends_only_declared_parts(tmp_path):
    code, records, seen = run_composed(tmp_path, BLOCKS, capabilities={})
    assert code == 0, records
    parts = next(c["observed"] for c in records[0]["checks"] if c["name"] == "composed_parts")
    assert (parts["beta_header"], parts["thinking"], parts["effort"]) == (False, None, None)
    assert (parts["attribution_line"], parts["inline_system"]) == (False, False)
    assert not any("anthropic-beta" in headers for _, _, headers in seen)


def test_repeat_cases_pass_where_the_composed_request_fails(tmp_path):
    with FakeFleet(cache_probe=True, cache_block=16, refuse_thinking_tools=True) as fleet:
        path = write_profile(tmp_path, fleet, responses=True)
        raw = yaml.safe_load(path.read_text())
        raw["ingresses"][0]["surfaces"] = ["messages"]
        raw["capabilities"] = {
            "cache_reuse": {"surfaces": ["messages"], "prefix_words": 128, **BLOCKS}
        }
        path.write_text(yaml.safe_dump(raw))
        code, records = run_suites(
            path, tmp_path / "run", "-k", "test_native_cache_reuse", "--llmdv-surface", "messages"
        )
    assert code == 0 and [r["outcome"] for r in records] == ["pass"], records
