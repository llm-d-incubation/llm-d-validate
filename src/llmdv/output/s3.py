"""Publication to S3 or an S3-compatible store, with AWS or IBM IAM authentication.

AWS credentials come from boto3's default chain; IBM IAM credentials come from the environment.
Neither belongs in the profile or on the command line.
"""

from __future__ import annotations

import mimetypes
import os
from pathlib import Path

from ..http import redact
from ..log.setup import get_logger
from .remote import (
    DELIVERY,
    WRITE_CHECK_KEY,
    Patience,
    RemoteTarget,
    UnsupportedRemote,
    publish_files,
)

# Not "output.*": that would nest under the primary-output logger, printing to stdout.
log = get_logger("sink.s3")


def _auth_mode() -> str:
    mode = os.environ.get("LLMDV_S3_AUTH", "aws").strip() or "aws"
    if mode not in {"aws", "ibm-iam"}:
        raise UnsupportedRemote("LLMDV_S3_AUTH must be aws or ibm-iam")
    return mode


def _register_credentials(credentials) -> None:
    """Register resolved AWS credentials for redaction, as the IBM IAM path already does.

    S3 echoes the access key id back in `InvalidAccessKeyId` and `SignatureDoesNotMatch`
    errors, and those messages reach `outcome.json` through `deliver()`'s `scrub()` -- which
    can only redact values it has been told about. Without this the registry is empty on the
    AWS path and the scrub is a no-op.

    Taken from the resolved credentials rather than from the environment: boto3's chain also
    reads ~/.aws/credentials, an SSO cache and instance metadata, and a secret that never
    passed through an environment variable is no less a secret.
    """
    if credentials is None:
        return
    frozen = credentials.get_frozen_credentials()
    redact.install_secrets(
        [value for value in (frozen.access_key, frozen.secret_key, frozen.token) if value]
    )


def missing_credentials_reason() -> str | None:
    """Why an unattended job cannot authenticate, or None if a provider has credentials.

    AWS uses its provider chain; IBM IAM uses explicitly configured environment variables.
    This does not probe S3 or hide errors from an authenticated upload.
    """
    if _auth_mode() == "ibm-iam":
        from .ibm_cos import missing_credentials_reason as ibm_missing_credentials

        return ibm_missing_credentials()

    try:
        import boto3
        from botocore.exceptions import BotoCoreError
    except ImportError:
        raise UnsupportedRemote(
            "s3:// publishing needs boto3; install it with: pip install 'llm-d-validate[s3]'"
        ) from None

    try:
        credentials = boto3.Session().get_credentials()
    except BotoCoreError as exc:
        # Provider errors can contain credential material or private URLs; record only the type.
        return f"AWS credentials could not be resolved ({type(exc).__name__})"
    _register_credentials(credentials)
    return "no AWS credentials were found" if credentials is None else None


class S3Sink:
    def __init__(self, target: RemoteTarget, patience: Patience | None = None) -> None:
        self.patience = patience or DELIVERY
        self.target = target
        bucket, _, prefix = target.location.partition("/")
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self._s3 = None

    def describe(self) -> str:
        return self.target.uri

    def _key(self, *parts: str) -> str:
        return "/".join(part.strip("/") for part in (self.prefix, *parts) if part)

    def _client(self):
        if self._s3 is not None:
            return self._s3
        if _auth_mode() == "ibm-iam":
            from .ibm_cos import create_client

            self._s3 = create_client(self.patience)
            return self._s3
        try:
            import boto3
            from botocore.config import Config
        except ImportError:
            raise UnsupportedRemote(
                "s3:// publishing needs boto3; install it with: pip install 'llm-d-validate[s3]'"
            ) from None

        endpoint = os.environ.get("AWS_ENDPOINT_URL_S3") or os.environ.get("AWS_ENDPOINT_URL")
        config = Config(
            retries={"mode": "standard", "max_attempts": self.patience.attempts},
            connect_timeout=self.patience.connect,
            read_timeout=self.patience.read,
            # botocore >= 1.36 otherwise sends aws-chunked uploads with trailing checksums,
            # which several S3-compatible stores reject.
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
            # S3-compatible stores are generally reached by path; AWS itself decides.
            s3={"addressing_style": "path" if endpoint else "auto"},
        )
        # One session, so the credentials registered for redaction are the ones the client
        # will actually sign with.
        session = boto3.Session()
        _register_credentials(session.get_credentials())
        self._s3 = session.client("s3", config=config)
        return self._s3

    def key_for(self, *parts: str) -> str:
        """The key a run's file lives at. Public because reading a published run is a
        legitimate use, and the alternative was callers reaching into `_key`."""
        return self._key(*parts)

    def list_keys(self, prefix: str) -> list[str]:
        """Every key under a prefix, relative to it."""
        paginator = self._client().get_paginator("list_objects_v2")
        full = self._key(prefix)
        if not full.endswith("/"):
            full += "/"
        found: list[str] = []
        for page in paginator.paginate(Bucket=self.bucket, Prefix=full):
            for item in page.get("Contents") or []:
                found.append(item["Key"][len(full) :])
        return found

    def get_bytes(self, *parts: str) -> bytes:
        return self._client().get_object(Bucket=self.bucket, Key=self._key(*parts))["Body"].read()

    def put_bytes(self, data: bytes, *parts: str, content_type: str | None = None) -> str:
        key = self._key(*parts)
        kwargs = {"Bucket": self.bucket, "Key": key, "Body": data}
        if content_type:
            kwargs["ContentType"] = content_type
        self._client().put_object(**kwargs)
        return f"s3://{self.bucket}/{key}"

    def check(self) -> None:
        """Raise unless the destination is writable; run at preflight, before any case."""
        self._client().put_object(
            Bucket=self.bucket,
            Key=self._key(WRITE_CHECK_KEY),
            Body=b"llmdv preflight write check\n",
        )

    def publish(self, run_dir: Path, name: str) -> str:
        """Upload a frozen run directory under `<prefix>/<name>/`, MANIFEST.json last."""
        client = self._client()
        destination = self._key(name)

        def put(relpath: str) -> None:
            content_type = mimetypes.guess_type(relpath)[0] or "application/octet-stream"
            with (run_dir / relpath).open("rb") as body:
                client.put_object(
                    Bucket=self.bucket,
                    Key=f"{destination}/{relpath}",
                    Body=body,
                    ContentType=content_type,
                )

        count = publish_files(run_dir, put)
        location = f"s3://{self.bucket}/{destination}/"
        log.info("published %d files to %s", count, location)
        return location
