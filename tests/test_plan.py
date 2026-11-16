"""`llmdv plan` reports what `llmdv verify` then does, and touches nothing to find out.

The plan is pytest's own collection plus the setup questions a run asks first, so the test that
matters is the comparison: for each example profile, every case the plan says skips is skipped by
verify with the same class and reason, and every case verify ran was planned to run. Two
profiles are verified in full; the rest stop after setup (`--setup-only`), which is where every
skip the plan claims to know is decided, and are compared by count.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest
import yaml

from llmdv.profile import compose
from tests.fakes import SRC, FakeFleet, run_llmdv

ROOT = SRC.parent
FULL = ["deploy/overlays/example/profile.yaml", "profiles/examples/example-two-ingress.yaml"]
BY_COUNT = [
    "profiles/examples/example-gateway-only.yaml",
    "profiles/examples/example-three-ingress.yaml",
    "profiles/examples/example-two-ingress-staging.yaml",
]


def test_every_example_profile_is_compared():
    """Guards the lists: a new example must be added to one of them, or this goes vacuous."""
    examples = {str(p.relative_to(ROOT)) for p in (ROOT / "profiles" / "examples").glob("*.yaml")}
    assert examples <= set(FULL + BY_COUNT)


# Real profiles name their address by variable; the examples are pointed at the fake the same way.
URL_ENV = "LLMDV_PLAN_TEST_URL"


def _raw(example: str) -> dict:
    """The example as one mapping, with any `extends` parents merged in, as the loader sees it."""
    raw = compose.composed(compose.layers(ROOT / example))
    raw.pop("extends", None)
    return raw


def _pointed(example: str, tmp_path: Path) -> Path:
    """The example profile with every ingress's address read from URL_ENV, nothing else changed."""
    raw = _raw(example)
    for ingress in raw["ingresses"]:
        ingress.pop("base_url", None)
        ingress["base_url_env"] = URL_ENV
    path = tmp_path / "profile.yaml"
    path.write_text(yaml.safe_dump(raw))
    return path


def _plan(profile: Path, *extra: str, env: dict[str, str] | None = None) -> dict:
    result = run_llmdv("plan", "--profile", str(profile), "--json", *extra, env=env)
    assert result.returncode == 0, result.stderr
    document = json.loads(result.stdout)
    assert document["schema"] == "llmdv.plan/1"
    return document


def _verify(profile: Path, run_dir: Path, base_url: str, *extra: str) -> list[dict]:
    run_llmdv(
        "verify", "--profile", str(profile), "--local-output", str(run_dir),
        "--skip-preflight", "--pod-logs", "off", *extra,
        env={URL_ENV: base_url, "KUBECONFIG": str(run_dir / "no-kubeconfig")},
    )  # fmt: skip
    results = run_dir / "results.jsonl"
    return [json.loads(line) for line in results.read_text().splitlines() if line.strip()]


def _model(example: str) -> str:
    return _raw(example)["model"]["public_id"]


@pytest.mark.parametrize("example", FULL)
def test_the_plan_is_what_verify_records(example, tmp_path):
    with FakeFleet(model=_model(example)) as fleet:
        profile = _pointed(example, tmp_path)
        plan = _plan(profile, env={URL_ENV: fleet.base_url})
        records = _verify(profile, tmp_path / "run", fleet.base_url)
        records = {record["nodeid"]: record for record in records}

    rows = {row["nodeid"]: row for row in plan["cases"]}
    assert set(rows) == set(records), "plan and verify collected different cases"
    for nodeid, row in rows.items():
        record = records[nodeid]
        if row["outcome"] == "skip":
            assert (record["outcome"], record["skip_class"], record["reason"]) == (
                "skip",
                row["skip_class"],
                f"Skipped: {row['reason']}",
            ), nodeid
        else:
            # Ran, or skipped from inside for a reason only the run decides.
            assert record["outcome"] in ("pass", "fail", "error", "skip"), nodeid
    ran = {nodeid for nodeid, record in records.items() if record["outcome"] != "skip"}
    assert ran <= {nodeid for nodeid, row in rows.items() if row["outcome"] == "run"}


@pytest.mark.parametrize("example", BY_COUNT)
def test_the_plan_counts_what_verify_skips_at_setup(example, tmp_path):
    with FakeFleet(model=_model(example)) as fleet:
        profile = _pointed(example, tmp_path)
        plan = _plan(profile, env={URL_ENV: fleet.base_url})
        # --setup-only stops each case after setup, which is where every planned skip is decided.
        records = _verify(profile, tmp_path / "run", fleet.base_url, "--", "--setup-only")
        sent = len(fleet.seen)

    assert sent == 0, "setup alone sent requests; the comparison would not be setup-only"
    skipped = Counter(record["skip_class"] or "unclassified" for record in records)
    assert dict(skipped) == plan["counts"]["by_skip_class"]
    assert plan["counts"]["run"] == plan["counts"]["total"] - len(records)


def _profile(tmp_path: Path, base_url: str = "http://10.255.255.1:9") -> Path:
    """A valid two-ingress profile behind an unroutable address, with a credential never set."""
    raw = yaml.safe_load((ROOT / FULL[1]).read_text())
    for ingress in raw["ingresses"]:
        ingress.pop("base_url_env", None)
        ingress["base_url"] = base_url
        ingress["auth"] = {"type": "bearer", "key_env": "LLMDV_PLAN_TEST_KEY_NEVER_SET"}
    path = tmp_path / "profile.yaml"
    path.write_text(yaml.safe_dump(raw))
    return path


# Any attempt to resolve a name or open a connection ends the process with a code of its own,
# so a swallowed transport error cannot hide one.
_NO_NETWORK = """
import os, socket, sys
def refuse(*args, **kwargs):
    sys.stderr.write(f"network use: {args!r}\\n")
    os._exit(97)
socket.socket.connect = socket.socket.connect_ex = refuse
socket.create_connection = socket.getaddrinfo = refuse
from llmdv.cli.main import main
sys.exit(main(sys.argv[1:]))
"""


def test_plan_needs_no_network_and_no_credentials(tmp_path):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("LLMDV_", "KUBE"))}
    env.update(PYTHONPATH=str(SRC), KUBECONFIG=str(tmp_path / "none"))
    result = subprocess.run(  # noqa: S603 - fixed interpreter and args
        [sys.executable, "-c", _NO_NETWORK, "plan", "--profile", str(_profile(tmp_path)), "--json"],
        capture_output=True,
        text=True,
        cwd=ROOT,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    document = json.loads(result.stdout)
    assert document["counts"]["run"] > 0
    assert not (tmp_path / "runs").exists()


def test_the_document_carries_rows_and_counts(tmp_path):
    document = _plan(_profile(tmp_path), "-m", "discovery", "--ingress", "vllm")
    rows = document["cases"]
    assert rows and {row["ingress"] for row in rows} == {"vllm"}
    assert all("discovery" in row["markers"] for row in rows)
    assert set(rows[0]) >= {
        "nodeid", "case_id", "variant", "ingress", "markers", "outcome", "skip_class", "reason",
    }  # fmt: skip
    counts = document["counts"]
    assert counts["total"] == len(rows) == counts["run"] + counts["skip"]
    assert sum(sum(tally.values()) for tally in counts["by_suite"].values()) == counts["total"]
    assert document["selection"]["marker"] == "discovery"


def test_a_skip_names_its_class_and_reason(tmp_path):
    rows = _plan(_profile(tmp_path), "--ingress", "router")["cases"]
    skipped = [row for row in rows if row["outcome"] == "skip"]
    assert skipped and all(row["skip_class"] and row["reason"] for row in skipped)


def test_text_puts_counts_first_and_quiet_prints_only_counts(tmp_path):
    profile = _profile(tmp_path)
    full = run_llmdv("plan", "--profile", str(profile), "-m", "discovery")
    quiet = run_llmdv("plan", "--profile", str(profile), "-m", "discovery", "-q")
    assert full.returncode == quiet.returncode == 0, full.stderr + quiet.stderr
    assert full.stdout.splitlines()[0].startswith("example-two-ingress: ")
    assert "## discovery" in full.stdout
    assert "## " not in quiet.stdout and "would run" in quiet.stdout


def test_an_unknown_marker_is_a_usage_error_listing_markers(tmp_path):
    result = run_llmdv("plan", "--profile", str(_profile(tmp_path)), "-m", "discovry")
    assert result.returncode == 2
    assert "no such marker" in result.stderr and "discovery" in result.stderr


def test_a_selector_matching_nothing_says_so(tmp_path):
    result = run_llmdv("plan", "--profile", str(_profile(tmp_path)), "-k", "matches_no_case")
    assert result.returncode == 2
    assert "no tests were collected" in result.stderr


def test_an_unknown_ingress_is_a_usage_error(tmp_path):
    result = run_llmdv("plan", "--profile", str(_profile(tmp_path)), "--ingress", "nope")
    assert result.returncode == 2
    assert "--ingress 'nope'" in result.stderr


def test_an_invalid_profile_is_a_usage_error(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text("schema: llmdv.profile/1\nname: bad\ningresses: not-a-list\n")
    assert run_llmdv("plan", "--profile", str(path)).returncode == 2
    assert run_llmdv("plan", "--profile", str(tmp_path / "missing.yaml")).returncode == 2
