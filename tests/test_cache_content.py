"""Content reuse cases, each proven to fail on the defect it targets."""

import pytest

from tests.test_cache_repeats import SURFACES
from tests.test_cache_reuse import BLOCKS, RATIO, run_cache_case


def kind(record):
    return record["variant"].rpartition("/")[2]


def outcomes(records):
    return {kind(r): r["outcome"] for r in records}


@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize(
    "declaration,defect,expected",
    [
        (BLOCKS, None, {"multilingual": "pass", "long_line": "pass"}),
        (RATIO, None, {"multilingual": "pass", "long_line": "pass"}),
        # Each defect places only its own content blind, so a plain repeat cannot see it.
        (BLOCKS, "non_ascii_blind", {"multilingual": "fail", "long_line": "pass"}),
        (BLOCKS, "long_line_blind", {"multilingual": "pass", "long_line": "fail"}),
        (RATIO, "long_line_blind", {"multilingual": "pass", "long_line": "fail"}),
    ],
)
def test_content_reuse_needs_a_tokenizer_that_agrees(
    tmp_path, surface, declaration, defect, expected
):
    code, records = run_cache_case(tmp_path, surface, declaration, "test_content_reuse", defect)
    assert outcomes(records) == expected, records
    assert code == (1 if "fail" in expected.values() else 0), records


@pytest.mark.parametrize("defect", ["non_ascii_blind", "long_line_blind"])
def test_repeat_cases_miss_the_content_defects(tmp_path, defect):
    code, records = run_cache_case(tmp_path, "chat_completions", BLOCKS, "test_native", defect)
    assert code == 0 and {r["outcome"] for r in records} == {"pass"}, records


def test_content_prompts_carry_the_shape_they_name(tmp_path):
    code, records = run_cache_case(tmp_path, "messages", BLOCKS, "test_content_reuse")
    assert code == 0, records
    shapes = {
        kind(r): next(c["observed"] for c in r["checks"] if c["name"] == "content_shape")
        for r in records
    }
    assert shapes["multilingual"]["non_ascii_chars"] > 0, shapes
    assert shapes["long_line"]["non_ascii_chars"] == 0, shapes
    assert shapes["long_line"]["longest_line_chars"] > 500, shapes
