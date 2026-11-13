"""The fake servers stay silent when a client hangs up, and still report anything else.

A parallel CI run printed a BrokenPipeError traceback from a FakeFleet request thread: a client
closed the connection before the reply was written. No test failed, but the traceback looked
like a crash in the log.
"""

from __future__ import annotations

import pytest

from tests.transport_fakes import QuietServer


@pytest.fixture
def server():
    srv = QuietServer(("127.0.0.1", 0), None)
    yield srv
    srv.server_close()


@pytest.mark.parametrize("error", [BrokenPipeError, ConnectionResetError, ConnectionAbortedError])
def test_a_client_that_hung_up_prints_nothing(server, capsys, error):
    try:
        raise error(32, "gone")
    except error:
        server.handle_error(None, ("127.0.0.1", 1))
    assert capsys.readouterr().err == ""


def test_any_other_error_is_still_printed(server, capsys):
    try:
        raise ValueError("a real defect in the fake")
    except ValueError:
        server.handle_error(None, ("127.0.0.1", 1))
    assert "ValueError: a real defect in the fake" in capsys.readouterr().err
