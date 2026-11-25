"""Publication never includes the target of a source link outside the run."""

from __future__ import annotations

import hashlib
import json
import os
from argparse import Namespace

import pytest

from llmdv.cli import publish_cmd
from llmdv.output import manifest
from llmdv.output.remote import IncompletePublication, parse_remote_uri, sink_for
from llmdv.output.s3 import S3Sink


class MemoryS3:
    def __init__(self):
        self.objects = {}

    def put_object(self, *, Bucket, Key, Body, **kwargs):
        self.objects[Key] = Body.read() if hasattr(Body, "read") else Body


@pytest.fixture(params=["file", "s3"])
def publication(tmp_path, request):
    archive = tmp_path / "archive"
    if request.param == "file":
        sink = sink_for(archive.as_uri())

        def objects():
            base = archive / "profile" / "run"
            return {
                p.relative_to(base).as_posix(): p.read_bytes()
                for p in base.rglob("*")
                if p.is_file()
            }

    else:
        sink = S3Sink(parse_remote_uri("s3://example/archive"))
        client = MemoryS3()
        sink._s3 = client

        def objects():
            return {
                key.removeprefix("archive/profile/run/"): value
                for key, value in client.objects.items()
            }

    return sink, objects


@pytest.mark.parametrize("link", ["file", "directory", "dangling", "loop", "manifest"])
def test_unsafe_source_stops_publication_before_any_upload(tmp_path, publication, link):
    run = tmp_path / "run"
    run.mkdir()
    (run / "a-evidence.txt").write_bytes(b"valid evidence")
    external = tmp_path / "external"
    external.mkdir()
    secret = external / "private.txt"
    secret.write_bytes(b"synthetic private content")
    if link == "directory":
        (run / "z-linked").symlink_to(external, target_is_directory=True)
    elif link == "dangling":
        (run / "z-linked").symlink_to(run / "missing")
    elif link == "loop":
        (run / "z-linked").symlink_to("z-linked")
    else:
        (run / (manifest.FILENAME if link == "manifest" else "z-linked")).symlink_to(secret)

    sink, objects = publication
    with pytest.raises(ValueError, match="publication source"):
        sink.publish(run, "profile/run")

    assert objects() == {}
    assert secret.read_bytes() == b"synthetic private content"
    assert not (run / manifest.FILENAME).exists() or (run / manifest.FILENAME).is_symlink()


def test_contained_source_links_preserve_names_bytes_and_manifest(tmp_path, publication):
    run = tmp_path / "run"
    (run / "nested").mkdir(parents=True)
    content = b"ordinary run evidence"
    (run / "nested" / "evidence.txt").write_bytes(content)
    (run / "linked.txt").symlink_to("nested/evidence.txt")
    (run / "linked-dir").symlink_to("nested", target_is_directory=True)
    sink, objects = publication

    sink.publish(run, "profile/run")

    uploaded = objects()
    body = json.loads(uploaded.pop(manifest.FILENAME))
    assert uploaded == {"linked.txt": content, "nested/evidence.txt": content}
    assert {entry["path"] for entry in body["files"]} == set(uploaded)
    for entry in body["files"]:
        assert entry["size"] == len(content)
        assert entry["sha256"] == hashlib.sha256(content).hexdigest()


def test_replaced_source_is_rechecked_before_upload(tmp_path, publication, monkeypatch):
    run = tmp_path / "run"
    run.mkdir()
    source = run / "evidence.txt"
    source.write_bytes(b"run evidence")
    external = tmp_path / "private.txt"
    external.write_bytes(b"synthetic private content")
    write_manifest = manifest.write

    def replace_after_hashing(path):
        files = write_manifest(path)
        source.unlink()
        source.symlink_to(external)
        return files

    monkeypatch.setattr(manifest, "write", replace_after_hashing)
    sink, objects = publication
    with pytest.raises(IncompletePublication, match="publication source escapes"):
        sink.publish(run, "profile/run")

    assert objects() == {}


def test_manifest_replacement_does_not_follow_a_link_added_after_validation(tmp_path, monkeypatch):
    run = tmp_path / "run"
    run.mkdir()
    (run / "evidence.txt").write_bytes(b"run evidence")
    external = tmp_path / "private.txt"
    external.write_bytes(b"synthetic private content")
    source_files = manifest.source_files

    def insert_link_after_validation(path):
        files = source_files(path)
        (path / manifest.FILENAME).symlink_to(external)
        return files

    monkeypatch.setattr(manifest, "source_files", insert_link_after_validation)
    manifest.write(run)

    assert external.read_bytes() == b"synthetic private content"
    assert not (run / manifest.FILENAME).is_symlink()
    assert json.loads((run / manifest.FILENAME).read_text())["files"][0]["path"] == "evidence.txt"


def test_source_root_can_be_reached_through_a_symlink(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    (run / "evidence.txt").write_bytes(b"run evidence")
    alias = tmp_path / "alias"
    alias.symlink_to(run, target_is_directory=True)

    assert manifest.write(alias) == ["evidence.txt"]


@pytest.mark.parametrize("entry", ["command", "library"])
@pytest.mark.parametrize("scheme", ["file", "s3"])
@pytest.mark.parametrize("linked", ["outcome.json", ".outcome.json.tmp", "dashboard.html"])
def test_publish_rejects_links_before_rewriting_evidence(
    tmp_path, monkeypatch, entry, scheme, linked
):
    run = tmp_path / "run"
    run.mkdir()
    body = {"profile": {"name": "profile"}, "run_id": "run", "verification": {"exit_code": 0}}
    (run / "outcome.json").write_text(json.dumps(body))
    external = tmp_path / "external.json"
    external.write_text(json.dumps({**body, "private_note": "synthetic external content"}))
    original = external.read_bytes()
    link = run / linked
    link.unlink(missing_ok=True)
    link.symlink_to(external)

    def unexpected(*args, **kwargs):
        pytest.fail("unsafe sources must be rejected before recovery, analysis or delivery")

    for name in ("_missing_credentials", "_unusable", "deliver"):
        monkeypatch.setattr(publish_cmd, name, unexpected)
    monkeypatch.setattr(publish_cmd.dashboard, "write_best_effort", unexpected)
    uri = (tmp_path / "archive").as_uri() if scheme == "file" else "s3://example/archive"
    if entry == "command":
        code = publish_cmd.cmd_publish(
            Namespace(run_dir=run, remote_output=uri, exit_code=None, analyze=True)
        )
    else:
        code = publish_cmd.publish_run(run, uri, None, analyze=True)

    assert code == 5
    assert link.is_symlink()
    assert external.read_bytes() == original
    assert not (run / manifest.FILENAME).exists()
    assert not (tmp_path / "archive").exists()


@pytest.mark.parametrize("recorded,supplied,expected", [(1, None, 1), (0, 1, 5), (None, 1, 1)])
def test_source_rejection_preserves_a_safe_verification_exit(
    tmp_path, recorded, supplied, expected
):
    run = tmp_path / "run"
    run.mkdir()
    (run / "outcome.json").write_text(json.dumps({"verification": {"exit_code": recorded}}))
    external = tmp_path / "external.txt"
    external.write_text("synthetic external content")
    (run / "linked.txt").symlink_to(external)

    assert publish_cmd.publish_run(run, (tmp_path / "archive").as_uri(), supplied) == expected
    assert not (tmp_path / "archive").exists()


def test_rejected_outcome_cannot_override_the_supplied_exit(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    external = tmp_path / "external.json"
    external.write_text(json.dumps({"verification": {"exit_code": 0}}))
    (run / "outcome.json").symlink_to(external)

    assert publish_cmd.publish_run(run, (tmp_path / "archive").as_uri(), 1) == 1
    assert (run / "outcome.json").is_symlink()


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="requires FIFO support")
def test_rejected_non_regular_outcome_is_not_opened_for_verdict_recovery(tmp_path, monkeypatch):
    run = tmp_path / "run"
    run.mkdir()
    os.mkfifo(run / "outcome.json")

    def unexpected(*args, **kwargs):
        pytest.fail("a rejected FIFO must not be opened while recovering the verdict")

    monkeypatch.setattr(publish_cmd.outcome_file, "read", unexpected)
    assert publish_cmd.publish_run(run, (tmp_path / "archive").as_uri(), None) == 5
    assert not (tmp_path / "archive").exists()
