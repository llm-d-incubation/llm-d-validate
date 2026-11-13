"""An operator-pinned Claude Code binary drives the native Messages API through a recorder."""

import os
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from ...clients.claude_code import invoke, isolated_env
from ...clients.relay import ClientRelay
from ...evidence import journal
from .helpers import (
    check_client_result,
    check_native_generation,
    check_upstream,
    check_version,
)


@pytest.mark.surface("messages")
@pytest.mark.case_id("real_clients.claude_code_smoke")
def test_claude_code_smoke(client, surface, model, ingress, client_binary):
    label, binary = client_binary
    if (
        os.name != "posix"
        or not binary.executable.is_file()
        or not os.access(binary.executable, os.X_OK)
    ):
        pytest.skip("[requires_instrumentation] install the declared executable on a POSIX runner")
    with TemporaryDirectory(prefix="llmdv-client-") as temporary, ClientRelay(client) as relay:
        directory = Path(temporary)
        env = isolated_env(directory, relay.base_url, model.public_id)
        journal.record("client_binary", label)
        check_version(binary, directory, env)
        result = invoke(
            binary,
            directory=directory,
            env=env,
            prompt="Reply with the word ok.",
            timeout=min(ingress.timeouts.request, 120),
        )
        # The relay's evidence and the fleet's status come before the client's: a client the
        # fleet failed exits in error, often after a retry the relay refused. See check_upstream.
        ok = len(relay.generations) == 1 and relay.attempts == 1 and not relay.rejected
        journal.record(
            "client_generation_attempts",
            {
                "forwarded": len(relay.generations),
                "attempts": relay.attempts,
                "rejected": relay.rejected,
            },
            "exactly one generation",
            ok=ok,
        )
        check_upstream(relay, ingress, "client_upstream_statuses")
        check_client_result(result, model)
        assert ok, "client made no generation, retried, or requested an unsupported route"
        check_native_generation(relay.generations[0], surface, model, ingress)
