from __future__ import annotations

import hashlib

from llmdv.output.offload import Offloader, reference, whole
from llmdv.output.remote import UnsupportedRemote, parse_remote_uri


def test_file_offloader_stores_under_run_prefix(tmp_path):
    data = b"whole transcript\n"
    result = Offloader(f"file://{tmp_path}", "profile/run").store("logs/pod.log", data)

    assert result == {
        "status": "stored",
        "uri": f"file://{tmp_path}/profile/run/offloaded/logs/pod.log",
    }
    assert (tmp_path / "profile/run/offloaded/logs/pod.log").read_bytes() == data


def test_offloader_without_sink_is_explicitly_not_configured():
    assert Offloader(None, "profile/run").store("x", b"x") == {
        "status": "not_configured",
        "error": "pass --artifact-sink to keep it whole",
    }


def test_offloader_circuit_breaker_scrubs_and_skips(tmp_path):
    root = tmp_path / "not-a-directory"
    root.write_text("file")
    offloader = Offloader(f"file://{root}", "profile/run")

    failed = offloader.store("x", b"secret")
    skipped = offloader.store("y", b"secret")

    assert failed["status"] == "failed"
    assert "not-a-directory" in failed["error"]
    assert skipped["status"] == "skipped"
    assert skipped["error"].startswith("an earlier upload failed:")


def test_reference_and_whole_cover_upload_states():
    data = b"0123456789"
    ref = reference(data, 4, {"status": "failed", "error": "nope"})
    assert ref["bytes"] == 10
    assert ref["sha256"] == hashlib.sha256(data).hexdigest()
    assert ref["kept_bytes"] == 4
    assert ref["truncated"] is True
    assert whole(ref) is False
    for status in ("not_configured", "failed", "skipped"):
        assert not whole(reference(data, 4, {"status": status}))
    assert whole(reference(data, 10, {"status": "not_configured"})) is True
    assert whole(reference(data, 4, {"status": "stored", "uri": "file:///x"})) is True


def test_parse_remote_uri_uses_the_calling_flag():
    try:
        parse_remote_uri("pvc://results", flag="--artifact-sink")
    except UnsupportedRemote as exc:
        assert "--artifact-sink" in str(exc)
    else:
        raise AssertionError("unsupported URI was accepted")
