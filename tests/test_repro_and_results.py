"""The generated curl, and the results relation a matrix is a pivot of."""

from __future__ import annotations

import json

from llmdv.http import redact
from llmdv.http.client import Exchange
from llmdv.http.repro import to_curl
from llmdv.profile.schema import Auth, AuthType
from llmdv.results.record import SCHEMA, ResultRecord, ResultsWriter, new_run_id


def an_exchange(**kw) -> Exchange:
    base = dict(
        method="POST",
        url="http://localhost:18180/v1/chat/completions",
        request_headers={"Content-Type": "application/json", "x-request-id": "llmdv-1"},
        request_body={"model": "m", "messages": [{"role": "user", "content": "hi"}]},
        status=404,
    )
    base.update(kw)
    return Exchange(**base)


def test_curl_is_a_runnable_script_with_the_body_as_a_heredoc():
    script = to_curl(an_exchange())
    assert script.startswith("#!/usr/bin/env bash")
    assert "set -euo pipefail" in script
    assert "--data-binary @-" in script and "<<'BODY'" in script
    assert '"model": "m"' in script


def test_curl_carries_no_credential_and_reads_the_env_var_instead():
    """A bundle has to be shareable, so the script must reference the variable, not the key."""
    exchange = an_exchange(
        request_headers={"Authorization": redact.SENTINEL, "Content-Type": "application/json"}
    )
    script = to_curl(exchange, auth=Auth(type=AuthType.BEARER, key_env="LLMDV_KEY"))
    assert redact.SENTINEL not in script
    assert '"Authorization: Bearer ${LLMDV_KEY}"' in script
    assert "LLMDV_KEY:?set LLMDV_KEY" in script


def test_curl_records_the_vantage_point():
    """The same base_url means different things through a forward and in-cluster."""
    script = to_curl(an_exchange(), setup_hint=["kubectl -n ns port-forward svc/x 18180:80"])
    assert "port-forward svc/x 18180:80" in script


def test_results_are_appended_and_flushed_per_record(tmp_path):
    """An interrupted suite must still leave the results it already produced."""
    path = tmp_path / "results.jsonl"
    writer = ResultsWriter(path)
    writer.append(
        ResultRecord(
            run_id="r", nodeid="n1", outcome="pass", profile="p", case_id="c.one", ingress="vllm"
        )
    )
    # Readable before close(), because the handle is line-buffered.
    assert json.loads(path.read_text().splitlines()[0])["case_id"] == "c.one"
    writer.append(
        ResultRecord(
            run_id="r", nodeid="n2", outcome="fail", profile="p", case_id="c.two", ingress="router"
        )
    )
    writer.close()
    assert writer.counts() == {"pass": 1, "fail": 1}


def test_record_carries_the_schema_version_and_a_timestamp():
    record = ResultRecord(run_id=new_run_id(), nodeid="n", outcome="pass", profile="p")
    payload = json.loads(record.to_json())
    assert payload["schema"] == SCHEMA
    assert payload["ts"].startswith("20")
    assert "served_by" in payload


def test_matrix_distinguishes_skip_classes_from_plain_skips(tmp_path):
    """Collapsing these would make the matrix read as coverage."""
    writer = ResultsWriter(tmp_path / "r.jsonl")
    writer.append(
        ResultRecord(
            run_id="r", nodeid="a", outcome="pass", profile="p", case_id="c", ingress="vllm"
        )
    )
    writer.append(
        ResultRecord(
            run_id="r",
            nodeid="b",
            outcome="skip",
            profile="p",
            case_id="c",
            ingress="router",
            skip_class="not_implemented",
        )
    )
    writer.close()
    assert writer.matrix()["c"] == {"vllm": "pass", "router": "skip:not_implemented"}


def test_matrix_keeps_each_variant_of_a_case_in_its_own_row(tmp_path):
    """A chat failure followed by a completions pass must not read as a single pass."""
    writer = ResultsWriter(tmp_path / "r.jsonl")
    for variant, outcome in (("chat_completions", "fail"), ("completions", "pass")):
        writer.append(
            ResultRecord(
                run_id="r",
                nodeid=variant,
                outcome=outcome,
                profile="p",
                case_id="c",
                variant=variant,
                ingress="vllm",
            )
        )
    writer.close()
    assert writer.matrix() == {
        "c[chat_completions]": {"vllm": "fail"},
        "c[completions]": {"vllm": "pass"},
    }


def _run_script(script: str, tmp_path, **env) -> str:
    """Run a generated repro script against a stand-in curl that prints what it was given."""
    import os
    import subprocess

    fake = tmp_path / "bin"
    fake.mkdir(exist_ok=True)
    (fake / "curl").write_text('#!/bin/sh\nfor a in "$@"; do printf "%s\\n" "$a"; done\ncat\n')
    (fake / "curl").chmod(0o755)
    path = tmp_path / "repro.sh"
    path.write_text(script)
    return subprocess.run(  # noqa: S603 - fixed interpreter and generated script
        ["bash", str(path)], capture_output=True, text=True, check=True,
        env={"PATH": f"{fake}:{os.environ['PATH']}", **env},
    ).stdout  # fmt: skip


def test_the_curl_script_really_sends_the_key_from_the_environment(tmp_path):
    """A string check passed while the script single-quoted the variable and sent it literally."""
    exchange = an_exchange(
        request_headers={"Authorization": redact.SENTINEL, "Content-Type": "application/json"}
    )
    script = to_curl(exchange, auth=Auth(type=AuthType.BEARER, key_env="LLMDV_KEY"))
    sent = _run_script(script, tmp_path, LLMDV_KEY="s3cret-value")
    assert "Authorization: Bearer s3cret-value" in sent.splitlines()
    assert "$LLMDV_KEY" not in sent


def test_the_curl_script_sends_an_api_key_header_from_the_environment(tmp_path):
    exchange = an_exchange(request_headers={"x-api-key": redact.SENTINEL})
    script = to_curl(exchange, auth=Auth(type=AuthType.API_KEY, key_env="LLMDV_KEY"))
    assert "x-api-key: s3cret-value" in _run_script(script, tmp_path, LLMDV_KEY="s3cret-value")
