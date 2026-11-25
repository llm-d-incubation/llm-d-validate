"""Dashboard replays send each recorded payload, independent of display truncation."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from llmdv.evidence import dashboard


def replay(tmp_path, script):
    """Execute the generated shell with an offline curl double capturing transmitted bytes."""
    executable = tmp_path / "curl"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json, pathlib, sys\n"
        "pathlib.Path('sent.body').write_bytes(sys.stdin.buffer.read())\n"
        "pathlib.Path('sent.args').write_text(json.dumps(sys.argv[1:]))\n"
    )
    executable.chmod(0o755)
    command = tmp_path / "replay.sh"
    command.write_text(script)
    result = subprocess.run(  # noqa: S603 -- fixed shell, generated script under test
        ["/bin/sh", str(command)],
        cwd=tmp_path,
        env={**os.environ, "PATH": f"{tmp_path}:{os.environ.get('PATH', '')}"},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    args = json.loads((tmp_path / "sent.args").read_text())
    assert args[args.index("--data-binary") + 1] == "@-"
    return (tmp_path / "sent.body").read_bytes()


@pytest.mark.parametrize(
    "body",
    [
        "",
        "no trailing newline",
        "two trailing newlines\n\n",
        "raw\x00bytes café",
        "quote ' and unicode café\nBODY\n$(touch injected)\n`touch injected`",
        {"model": "example", "prompt": "a" * 30000},
        "raw " * 10000,
    ],
)
def test_replay_preserves_complete_body_bytes_and_shell_literals(tmp_path, body):
    exchange = {"method": "POST", "url": "https://example.invalid/v1/test", "request_body": body}
    script = dashboard.curl_for(exchange, None, body_cap=10)
    expected = body if isinstance(body, str) else json.dumps(body, indent=2, ensure_ascii=False)
    assert replay(tmp_path, script) == expected.encode("utf-8")
    assert not (tmp_path / "injected").exists()
    assert "@request.json" not in script


def test_each_exchange_replays_its_own_body_after_dashboard_is_copied(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    (run / "results.jsonl").write_text(
        json.dumps({"schema": "llmdv.results/1", "nodeid": "case", "outcome": "fail"}) + "\n"
    )
    bodies = [{"prompt": "first " * 5000}, {"prompt": "last " * 5000}]
    exchanges = [
        {"nodeid": "case", "method": "POST", "url": "https://example.invalid", "request_body": b}
        for b in bodies
    ]
    (run / "exchanges.jsonl").write_text("".join(json.dumps(e) + "\n" for e in exchanges))
    copied = tmp_path / "copied"
    copied.mkdir()
    data = dashboard.build(run, body_cap=10, max_exchanges=6)
    for item, body in zip(data["tests"][0]["exchanges"], bodies, strict=True):
        assert "truncated" in item["request_body"]
        sent = replay(copied, item["curl"])
        assert json.loads(sent) == body
