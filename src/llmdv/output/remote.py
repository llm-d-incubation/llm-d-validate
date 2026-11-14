"""Remote publication: `file://` and `s3://`, both under `<destination>/<name>/`, manifest last.

The destination is checked at preflight, so an unusable one fails before a long run, not after.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ..http.redact import scrub
from ..log.setup import get_logger
from . import manifest
from .base import ObjectSink

# Not "output.*": that would nest under the primary-output logger, printing to stdout.
log = get_logger("sink.remote")

SCHEMES = ("file", "s3")
WRITE_CHECK_KEY = ".llmdv-write-check"


@dataclass(frozen=True)
class Patience:
    """How long a store is given before we stop waiting.

    An upload is the run's whole point: results that are not delivered may as well not exist,
    so it waits. A read made to enrich a report is not, and inheriting an uploader's patience
    is how an advisory step turns a flapping store into a slow nightly. Naming the two stops
    one borrowing the other's timeouts by default.
    """

    attempts: int = 5
    connect: int = 10
    read: int = 60


#: Delivering results. Worth waiting for.
DELIVERY = Patience()

#: Reading something a report would merely be better for. Give up quickly and say so.
GLANCE = Patience(attempts=1, connect=3, read=10)


class UnsupportedRemote(Exception):
    pass


class IncompletePublication(Exception):
    """Some files could not be published, so the manifest was withheld."""


def publish_files(run_dir: Path, put: Callable[[str], None]) -> int:
    """Publish every file with `put(relpath)`, then MANIFEST.json only if all succeeded.

    One failure does not stop the rest, so a failing run still gets out as much as it can.
    """
    files = manifest.write(run_dir)
    failed: list[tuple[str, Exception]] = []
    for relpath in files:
        try:
            put(relpath)
        except Exception as exc:  # noqa: BLE001 -- keep going; report them all below
            failed.append((relpath, exc))
    if failed:
        first_path, first_error = failed[0]
        raise IncompletePublication(
            f"{len(failed)} of {len(files)} files failed to upload in this attempt (first: "
            f"{first_path}: {first_error}); MANIFEST.json was withheld"
        )
    put(manifest.FILENAME)
    return len(files) + 1


def deliver(run_dir: Path, uri: str, name: str) -> dict[str, Any]:
    """Publish a run under `<uri>/<name>/`; return its publication record. Never raises."""
    record: dict[str, Any] = {"destination": uri}
    try:
        location = sink_for(uri).publish(run_dir, name)
        record.update(status="published", location=location)
    except Exception as exc:  # noqa: BLE001 -- any delivery error means "not published"
        record.update(status="failed", error=scrub(str(exc)))
    return record


@dataclass(frozen=True)
class RemoteTarget:
    scheme: str
    location: str
    uri: str


def parse_remote_uri(uri: str, flag: str = "--remote-output") -> RemoteTarget:
    """Validate a remote URI's shape, raising if it can never work."""
    parts = urlsplit(uri)
    if not parts.scheme:
        raise UnsupportedRemote(
            f"{flag} {uri!r} has no scheme; expected one of: {', '.join(SCHEMES)}"
        )
    scheme = parts.scheme.lower()
    if scheme not in SCHEMES:
        raise UnsupportedRemote(
            f"{flag} scheme {scheme!r} is not recognised; known schemes: {', '.join(SCHEMES)}"
        )
    if scheme == "s3" and not parts.netloc:
        raise UnsupportedRemote(f"{flag} {uri!r} names no bucket; expected s3://bucket/")
    location = f"{parts.netloc}{parts.path}" if parts.netloc else parts.path
    return RemoteTarget(scheme=scheme, location=location, uri=uri)


class FileSink:
    """Copy a finished run directory to another path on disk."""

    def __init__(self, target: RemoteTarget) -> None:
        self.target = target

    def describe(self) -> str:
        return self.target.uri

    def check(self) -> None:
        root = Path(self.target.location).expanduser()
        root.mkdir(parents=True, exist_ok=True)
        probe = root / WRITE_CHECK_KEY
        probe.write_bytes(b"llmdv preflight write check\n")
        probe.unlink(missing_ok=True)

    def put_bytes(self, data: bytes, *parts: str, content_type: str | None = None) -> str:
        target = Path(self.target.location).expanduser().joinpath(*parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        return target.as_uri()

    def publish(self, run_dir: Path, name: str) -> str:
        destination = Path(self.target.location).expanduser() / name

        def put(relpath: str) -> None:
            target = destination / relpath
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(run_dir / relpath, target)

        publish_files(run_dir, put)
        log.info("published run to %s", destination)
        return str(destination)


def sink_for(uri: str, patience: Patience | None = None) -> ObjectSink:
    target = parse_remote_uri(uri)
    if target.scheme == "s3":
        from .s3 import S3Sink

        return S3Sink(target, patience=patience)
    return FileSink(target)
