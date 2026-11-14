"""IBM COS IAM client configuration; publication stays in the shared S3 sink."""

from __future__ import annotations

import os
from urllib.parse import quote_plus

from ..http import redact
from .remote import UnsupportedRemote

API_KEY_ENV = "IBM_COS_API_KEY"
INSTANCE_ENV = "IBM_COS_SERVICE_INSTANCE_ID"
AUTH_ENDPOINT = "https://iam.cloud.ibm.com/identity/token"


def _register_secrets(values: list[str]) -> None:
    # IAM sends form-encoded credentials; errors can echo either representation.
    redact.install_secrets(values + [quote_plus(value) for value in values])


def _credentials() -> dict[str, str]:
    credentials = {key: os.environ.get(key, "").strip() for key in (API_KEY_ENV, INSTANCE_ENV)}
    _register_secrets(list(credentials.values()))
    return credentials


def _missing(credentials: dict[str, str]) -> str | None:
    missing = [f"${key}" for key, value in credentials.items() if not value]
    return f"IBM IAM credentials missing: {', '.join(missing)}" if missing else None


def missing_credentials_reason() -> str | None:
    """Check explicit credentials locally, without contacting IAM or discovering other keys."""
    return _missing(_credentials())


def create_client(patience=None):
    from .remote import DELIVERY

    patience = patience or DELIVERY
    credentials = _credentials()
    if reason := _missing(credentials):
        raise UnsupportedRemote(reason)
    endpoint = os.environ.get("IBM_COS_ENDPOINT", "").strip()
    if not endpoint:
        raise UnsupportedRemote("IBM IAM publishing requires $IBM_COS_ENDPOINT")

    try:
        import ibm_boto3
        from ibm_botocore.config import Config
        from ibm_botocore.credentials import DefaultTokenManager
    except ImportError:
        raise UnsupportedRemote(
            "IBM IAM publishing needs ibm-cos-sdk; "
            "install it with: pip install 'llm-d-validate[ibm-cos]'"
        ) from None

    manager = DefaultTokenManager(
        api_key_id=credentials[API_KEY_ENV],
        service_instance_id=credentials[INSTANCE_ENV],
        auth_endpoint=os.environ.get("IBM_COS_AUTH_ENDPOINT", "").strip() or AUTH_ENDPOINT,
    )
    authenticate = manager.auth_function

    def authenticate_redacted():
        # The SDK owns token caching, refresh and bounded IAM retries. Scrub failures before
        # its exception logging, and register both issued tokens before it can use them.
        try:
            data = authenticate()
        except Exception as exc:
            raise RuntimeError(f"IBM IAM authentication failed: {redact.scrub(str(exc))}") from None
        _register_secrets([data.get("access_token", ""), data.get("refresh_token", "")])
        return data

    manager.auth_function = authenticate_redacted
    return ibm_boto3.client(
        "s3",
        token_manager=manager,
        ibm_service_instance_id=credentials[INSTANCE_ENV],
        endpoint_url=endpoint,
        config=Config(
            signature_version="oauth",
            retries={"mode": "standard", "max_attempts": patience.attempts},
            connect_timeout=patience.connect,
            read_timeout=patience.read,
            # Match the AWS path: avoid optional checksum trailers on compatible stores.
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
            s3={"addressing_style": "path"},
        ),
    )
