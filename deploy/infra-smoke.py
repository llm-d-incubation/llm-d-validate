"""Probe sent to the CBT container by infra-smoke.sh; no profile or inference calls."""

import json
import os
import tempfile
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path
from uuid import uuid4

from llmdv.cli.exit_codes import NOT_DELIVERED, OK, PREFLIGHT, USAGE
from llmdv.http.redact import install_secrets, scrub
from llmdv.log.setup import configure, emit, get_logger
from llmdv.output.remote import UnsupportedRemote, deliver, parse_remote_uri


def main() -> int:
    configure()
    log = get_logger("infra_smoke")
    # There is no fleet profile to register credentials. IBM's token manager also
    # registers its API key and issued tokens before it uses them.
    install_secrets(
        [os.environ.get(name, "") for name in (
            "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
        )]
    )  # fmt: skip
    destination = os.environ.get("LLMDV_RESULTS_URI", "").strip()
    try:
        if parse_remote_uri(destination).scheme != "s3":
            raise UnsupportedRemote("Infrastructure smoke requires an S3 LLMDV_RESULTS_URI")
    except UnsupportedRemote as exc:
        log.error("Infrastructure smoke configuration: %s", scrub(str(exc)))
        return USAGE

    try:
        if os.getuid() == 0:
            raise OSError("Infrastructure smoke must run as a non-root user")
        log.info("Infrastructure smoke: checking writable storage and S3 publication")
        with tempfile.TemporaryFile(dir="/tmp") as probe:
            probe.write(b"llmdv infrastructure smoke\n")
            probe.flush()

        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
        run_dir = Path(os.environ.get("LLMDV_WORK_DIR", "/work/runs")) / run_id
        run_dir.mkdir(parents=True)
        report = {
            "kind": "infrastructure-smoke",
            "run_id": run_id,
            "llmdv_version": version("llm-d-validate"),
            "uid": os.getuid(),
            "checks": {"non_root": "passed", "work_writable": "passed", "tmp_writable": "passed"},
            "functional_tests": "not_run",
        }
        (run_dir / "infrastructure.json").write_text(json.dumps(report, indent=2) + "\n")
    except OSError as exc:
        log.error("Infrastructure smoke environment: %s", scrub(str(exc)))
        return PREFLIGHT

    publication = deliver(run_dir, destination, f"infra-smoke/{run_id}")
    emit(json.dumps({**report, "publication": publication}, indent=2))
    return OK if publication["status"] == "published" else NOT_DELIVERED


if __name__ == "__main__":
    raise SystemExit(main())
