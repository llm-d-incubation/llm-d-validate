"""Features a real Claude Code session depends on, driven through the declared ingress.

Each case asserts that the feature worked, not that a response was well-formed: a layer that
drops part of what the client sends leaves every response valid and the feature dead.
"""

import sys
import uuid

import pytest

from ...assertions.images import assert_image_read
from ...clients.process import run_process
from ...evidence import journal
from ...surfaces.images import striped_image
from . import projects
from .sessions import SUBAGENT_TOOLS, compact_window, require_session, run_session

pytestmark = pytest.mark.surface("messages")

EDITING = ("Read", "Glob", "Grep", "Edit", "Write")
# Exact commands only. "Bash(python3 -m unittest:*)" would be a PREFIX rule, which also admits
# anything that merely starts with it, so it is not the boundary it looks like.
UNITTEST = (
    "Bash(python3 -m unittest)",
    "Bash(python3 -m unittest -v)",
)


def _check(name: str, observed, expected: str, ok: bool, message: str) -> None:
    journal.record(name, observed, expected, ok=ok)
    assert ok, message


@pytest.mark.case_id("real_clients.claude_code_skill")
def test_session_invokes_a_project_skill(client, model, ingress, client_binary):
    binary = require_session(ingress, client_binary, "skill")
    with run_session(
        client,
        model,
        binary,
        ingress=ingress,
        files=projects.skill_project(),
        prompt="Write the release notes for this project.",
        allowed_tools=(*EDITING, "Skill"),
    ) as session:
        used = [call["name"] for call in session.tool_uses()]
        _check(
            "skill_invoked",
            used,
            "the Skill tool among the calls",
            "Skill" in used,
            "the session never invoked the project skill that covers the request",
        )


@pytest.mark.case_id("real_clients.claude_code_hook")
def test_session_acts_on_hook_output(client, model, ingress, client_binary):
    binary = require_session(ingress, client_binary, "hook")
    marker = uuid.uuid4().hex[:12]
    with run_session(
        client,
        model,
        binary,
        ingress=ingress,
        files=projects.hook_project(),
        prompt="What is the current build id?",
        allowed_tools=("Read",),
        env={projects.HOOK_VARIABLE: marker},
    ) as session:
        _check(
            "hook_output_used",
            session.answer,
            f"an answer containing {marker}",
            marker in session.answer,
            "the answer does not contain the value only the hook supplied",
        )


@pytest.mark.case_id("real_clients.claude_code_parallel_reads")
def test_session_reads_several_files_in_one_turn(client, model, ingress, client_binary):
    binary = require_session(ingress, client_binary, "parallel_reads")
    with run_session(
        client,
        model,
        binary,
        ingress=ingress,
        files=projects.reads_project(),
        prompt="Read a.txt, b.txt and c.txt and tell me which has the most lines. "
        "Answer with the file name.",
        # Read only, so one search cannot stand in for three reads.
        allowed_tools=("Read",),
    ) as session:
        together = session.most_calls_in_one_turn("Read")
        ok = together >= 2 and "b.txt" in session.answer
        _check(
            "parallel_reads",
            {"most_reads_in_one_turn": together, "answer": session.answer},
            "two or more reads in one turn and the answer b.txt",
            ok,
            "the session read the files one turn at a time, or answered wrongly",
        )


@pytest.mark.case_id("real_clients.claude_code_nested_subagent")
def test_session_subagent_launches_a_subagent(client, model, ingress, client_binary):
    binary = require_session(ingress, client_binary, "nested_subagent")
    with run_session(
        client,
        model,
        binary,
        ingress=ingress,
        files=projects.subagent_project(),
        prompt="Launch a general-purpose subagent and tell it to launch its own Explore "
        "subagent to find which file defines zorblax_handler. Report the file name.",
        allowed_tools=(*EDITING, *SUBAGENT_TOOLS),
    ) as session:
        nested = [c["name"] for c in session.tool_uses(nested=True) if c["name"] in SUBAGENT_TOOLS]
        ok = bool(nested) and "gamma" in session.answer
        _check(
            "nested_subagent",
            {"launched_by_a_subagent": len(nested), "answer": session.answer},
            "a subagent launched by a subagent, and the answer gamma.py",
            ok,
            "no subagent launched its own subagent, or the answer was wrong",
        )


@pytest.mark.case_id("real_clients.claude_code_compaction")
def test_session_keeps_content_across_compaction(client, model, ingress, client_binary):
    binary = require_session(ingress, client_binary, "compaction")
    # Auto-compaction fires as the conversation approaches the MODEL's context window, so a
    # fleet that advertises no window gives the client nothing to measure against and it never
    # compacts. Checked before anything is sent: measured on a gateway reporting
    # max_model_len null, the session ran to 328k tokens against a declared 40k auto-compact
    # window and still produced no compact_boundary. Skipping here costs nothing; finding out
    # afterwards costs a full session.
    if model.max_model_len is None:
        pytest.skip(
            f"[undeclared] ingress {ingress.id!r} declares no max_model_len, so the client has "
            f"no context window to compact against"
        )
    codes = [uuid.uuid4().hex[:8] for _ in range(projects.COMPACTION_PARTS)]
    with run_session(
        client,
        model,
        binary,
        ingress=ingress,
        files=projects.compaction_project(codes),
        prompt="Read part1.txt to part6.txt one file at a time, in order, never two in the same "
        "turn. Each has one line with CODE=. When you have read all six, list the six codes.",
        allowed_tools=("Read",),
        # Derived from the declared context, not fixed. A window ABOVE the model's own limit can
        # never be reached -- the model's limit binds first -- so a hardcoded 40000 is dead on
        # any fleet whose context is smaller than that, and this case has no way to say so.
        # Half the declared context is below the limit on every fleet and small enough that six
        # reads plus the tool set should cross it.
        env={
            "CLAUDE_CODE_AUTO_COMPACT_WINDOW": str(compact_window(model)),
            # Read back by the offline stand-in so it can assert the window is below
            # the limit; the real client ignores it.
            "LLMDV_FAKE_CONTEXT": str(model.max_model_len),
        },
        # Measured on 2.1.289: with the tool set cut down to Read the client passed the window
        # without compacting, so this session keeps the full set and allows only Read.
        restrict_tools=False,
    ) as session:
        recalled = [code for code in codes if code in session.answer]
        # Compaction is the PRECONDITION, not the behaviour. Whether the client crosses its
        # window depends on the model's verbosity and the client version, and the author
        # already had to widen the tool set once to provoke it. A session that read everything
        # and lost nothing has not failed -- it has not been able to demonstrate anything, so
        # it skips the way background_cancellation does when the response finishes too fast.
        if session.compactions() < 1:
            peak = session.context_peak()
            journal.record(
                "compaction_recall",
                {
                    "compactions": 0,
                    "recalled": len(recalled),
                    "of": len(codes),
                    "context_peak": peak,
                    "declared_window": model.max_model_len,
                },
                "at least one compaction, so recall across it can be judged",
            )
            pytest.skip(
                f"[requires_load] the session answered without compacting, reaching "
                f"{peak} tokens against a declared window of {model.max_model_len} "
                f"({len(recalled)} of {len(codes)} codes recalled). Nothing is proven about "
                f"content surviving a compaction. A peak well above the window means the "
                f"client did not act on it, not that the conversation stayed small"
            )
        ok = len(recalled) == len(codes)
        _check(
            "compaction_recall",
            {"compactions": session.compactions(), "recalled": len(recalled), "of": len(codes)},
            "every code recalled across the compaction",
            ok,
            "the session compacted and lost values it had read before it did",
        )


@pytest.mark.case_id("real_clients.claude_code_multi_file_fix")
def test_session_fixes_bugs_across_files(client, model, ingress, client_binary):
    binary = require_session(ingress, client_binary, "multi_file_fix")
    files = projects.shop_project()
    with run_session(
        client,
        model,
        binary,
        ingress=ingress,
        files=files,
        prompt="Several tests fail under `python3 -m unittest`. Fix the bugs in the shop package "
        "without editing the tests, and confirm everything passes.",
        allowed_tools=(*EDITING, *UNITTEST),
    ) as session:
        untouched = (session.project / "tests/test_shop.py").read_text() == files[
            "tests/test_shop.py"
        ]
        tests = run_process(
            [sys.executable, "-m", "unittest"], cwd=session.project, env={}, timeout=60
        )
        ok = tests.code == 0 and tests.problem is None and untouched
        _check(
            "multi_file_fix",
            {"unittest_exit": tests.code, "tests_unedited": untouched},
            "the project's tests pass and were not edited",
            ok,
            "the project's tests still fail, or the session edited the tests",
        )


@pytest.mark.case_id("real_clients.claude_code_image_read")
def test_session_reads_an_image_file(client, model, ingress, client_binary):
    binary = require_session(ingress, client_binary, "image_read")
    picture = striped_image()
    with run_session(
        client,
        model,
        binary,
        ingress=ingress,
        files=projects.image_project(picture.png),
        prompt=f"Open the image file {projects.IMAGE_FILE} with the Read tool. "
        f"The image {picture.question}",
        # Read only, so the picture can reach the model one way: as the image in a tool result.
        allowed_tools=("Read",),
    ) as session:
        journal.record("session_tool_calls", [call["name"] for call in session.tool_uses()])
        assert_image_read(session.answer, picture.colours)
