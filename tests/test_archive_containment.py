"""Publication and offloads cannot escape their configured archive."""

from __future__ import annotations

import json

import pytest

from llmdv.cli.publish_cmd import publish_run
from llmdv.compare.archive import publish_one
from llmdv.output.offload import Offloader
from llmdv.output.paths import UnsafeName
from llmdv.output.remote import deliver, sink_for


@pytest.mark.parametrize(
    "name", ["../escaped/r", "/absolute/r", "p/../r", "p/r/extra", "p/", "p/\\r", "p/.hidden"]
)
@pytest.mark.parametrize("uri", ["file", "s3"])
def test_invalid_run_location_is_rejected_before_any_publication(tmp_path, name, uri):
    source = tmp_path / "source"
    source.mkdir()
    (source / "results.jsonl").write_text("evidence")
    archive = tmp_path / "archive"
    destination = archive.as_uri() if uri == "file" else "s3://example/archive"
    result = deliver(source, destination, name)
    assert result["status"] == "failed"
    assert "unsafe" in result["error"]
    assert not archive.exists()
    assert not (source / "MANIFEST.json").exists()


@pytest.mark.parametrize(
    "relpath", ["../escaped", "/absolute", "nested/../../escaped", "nested\\escaped"]
)
@pytest.mark.parametrize("scheme", ["file", "s3"])
def test_offloaded_paths_cannot_traverse(tmp_path, relpath, scheme):
    uri = tmp_path.as_uri() if scheme == "file" else "s3://example/archive"
    result = Offloader(uri, "profile/run").store(relpath, b"evidence")
    assert result["status"] == "failed"
    assert "unsafe object path" in result["error"]
    assert not list(tmp_path.iterdir())


def test_offloaded_run_identity_is_validated(tmp_path):
    result = Offloader(tmp_path.as_uri(), "../escaped/run").store("x", b"evidence")
    assert result["status"] == "failed"
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("operation", ["publish", "put", "publish_one", "check"])
def test_existing_destination_symlinks_cannot_write_outside_archive(tmp_path, operation):
    archive = tmp_path / "archive"
    outside = tmp_path / "outside"
    source = tmp_path / "source"
    for path in (archive, outside, source):
        path.mkdir()
    evidence = source / "results.jsonl"
    evidence.write_text("evidence")
    if operation == "check":
        (archive / ".llmdv-write-check").symlink_to(outside / "sentinel")
    else:
        (archive / "profile").symlink_to(outside, target_is_directory=True)
    sink = sink_for(archive.as_uri())
    with pytest.raises(UnsafeName):
        if operation == "publish":
            sink.publish(source, "profile/run")
        elif operation == "put":
            sink.put_bytes(b"evidence", "profile/run", "offloaded/x")
        elif operation == "publish_one":
            publish_one(archive.as_uri(), "profile", "run", evidence)
        else:
            sink.check()
    assert not list(outside.iterdir())


@pytest.mark.parametrize("field", ["profile", "run_id"])
def test_imported_outcome_cannot_escape_archive(tmp_path, field):
    source = tmp_path / "source"
    source.mkdir()
    body = {"profile": {"name": "profile"}, "run_id": "run", "verification": {"exit_code": 0}}
    if field == "profile":
        body["profile"]["name"] = "../escaped"
    else:
        body["run_id"] = "../escaped"
    (source / "outcome.json").write_text(json.dumps(body))
    archive = tmp_path / "archive"
    assert publish_run(source, archive.as_uri(), None) == 5
    assert not archive.exists()
    assert not (tmp_path / "escaped").exists()
    recorded = json.loads((source / "outcome.json").read_text())
    assert recorded["publication"]["status"] == "failed"
