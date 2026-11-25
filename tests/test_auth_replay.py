"""Replays retain credential shape without turning negative probes into valid requests."""

from __future__ import annotations

import base64
import json

import httpx
import pytest

from llmdv.evidence.dashboard import curl_for
from llmdv.http import auth_replay, redact
from llmdv.http.client import Exchange, RecordingClient, SdkRecorder
from llmdv.http.repro import to_curl
from llmdv.profile.schema import Auth, AuthType
from tests.test_repro_and_results import _run_script

KEY = "configured-profile-credential"
AUTH = Auth(type=AuthType.BEARER, key_env="LLMDV_REPLAY_KEY")


@pytest.fixture(autouse=True)
def registry():
    redact.reset_secrets()
    redact.install_secrets([KEY])
    yield
    redact.reset_secrets()


def exchange_with(headers):
    return Exchange(
        method="POST",
        url="https://example.invalid/v1/chat/completions",
        request_headers=redact.redact_headers(headers),
        request_auth=auth_replay.capture(headers, {"Authorization": f"Bearer {KEY}"}),
        request_body={"model": "example"},
    )


@pytest.mark.parametrize("renderer", ["bundle", "dashboard"])
@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"Authorization": "Bearer"}, "Authorization: Bearer"),
        ({"Authorization": "Bearer "}, "Authorization: Bearer "),
        ({"Authorization": "Basic"}, "Authorization: Basic"),
        ({"Authorization": "Basic "}, "Authorization: Basic "),
        (
            {"Authorization": "Basic bm8tc2VwYXJhdG9y"},
            "Authorization: Basic bGxtZHYtaW52YWxpZA==",
        ),
        ({"Authorization": "not-a-valid-key"}, "Authorization: llmdv-invalid"),
        ({"Authorization": "Bearer unknown-token"}, "Authorization: Bearer llmdv-invalid"),
        (
            {"Authorization": "Basic bGxtZHY6Ym9ndXM="},
            "Authorization: Basic bGxtZHY6bGxtZHYtaW52YWxpZA==",
        ),
        ({"Authorization": "Basic invalid?"}, "Authorization: Basic llmdv-invalid"),
        ({"Authorization": ""}, "Authorization;"),
        ({"x-api-key": ""}, "x-api-key;"),
        ({"x-api-key": "wrong-key"}, "x-api-key: llmdv-invalid"),
        ({"api-key": "wrong-key"}, "api-key: llmdv-invalid"),
    ],
)
def test_negative_credentials_keep_their_shape_without_requiring_a_key(
    tmp_path, renderer, headers, expected
):
    exchange = exchange_with(headers)
    script = (
        to_curl(exchange, AUTH)
        if renderer == "bundle"
        else curl_for(exchange.to_dict(), AUTH.key_env, 6000)
    )
    sent = _run_script(script, tmp_path)
    assert expected in sent.splitlines()
    assert "LLMDV_REPLAY_KEY" not in script
    assert KEY not in script
    serialized = json.dumps(exchange.to_dict())
    for value in headers.values():
        if len(value) > 10:
            assert value not in serialized


@pytest.mark.parametrize("renderer", ["bundle", "dashboard"])
@pytest.mark.parametrize("header", ["Authorization", "x-api-key", "api-key"])
def test_actual_profile_key_is_restored_in_the_original_header(tmp_path, renderer, header):
    value = f"Bearer {KEY}" if header == "Authorization" else KEY
    exchange = exchange_with({header: value})
    script = (
        to_curl(exchange, AUTH)
        if renderer == "bundle"
        else curl_for(exchange.to_dict(), AUTH.key_env, 6000)
    )
    sent = _run_script(script, tmp_path, LLMDV_REPLAY_KEY=KEY)
    assert f"{header}: {value}" in sent.splitlines()
    assert KEY not in json.dumps(exchange.to_dict())
    assert KEY not in script


@pytest.mark.parametrize("renderer", ["bundle", "dashboard"])
def test_missing_header_does_not_gain_credentials_or_require_environment(tmp_path, renderer):
    exchange = exchange_with({})
    script = (
        to_curl(exchange, AUTH)
        if renderer == "bundle"
        else curl_for(exchange.to_dict(), AUTH.key_env, 6000)
    )
    assert "LLMDV_REPLAY_KEY" not in script
    assert "Authorization" not in _run_script(script, tmp_path)


@pytest.mark.parametrize("renderer", ["bundle", "dashboard"])
def test_legacy_redacted_credentials_are_not_guessed_from_the_profile(renderer):
    exchange = Exchange(
        "GET", "https://example.invalid", request_headers={"Authorization": redact.SENTINEL}
    )
    script = (
        to_curl(exchange, AUTH)
        if renderer == "bundle"
        else curl_for(exchange.to_dict(), AUTH.key_env, 6000)
    )
    assert "exit 2" in script
    assert "provenance" in script
    assert "curl " not in script
    assert "${LLMDV_REPLAY_KEY}" not in script


@pytest.mark.parametrize("mode", ["request", "stream", "sdk"])
def test_all_capture_paths_record_provenance_before_discarding_credentials(mode):
    synthetic = "Basic " + base64.b64encode(b"llmdv:generated-invalid-token").decode()
    headers = {"Authorization": synthetic}
    if mode == "sdk":
        client = httpx.Client(
            transport=httpx.MockTransport(lambda request: httpx.Response(401, json={}))
        )
        recorder = SdkRecorder(client)
        try:
            client.post("https://example.invalid", headers=headers)
            exchange = recorder.last
        finally:
            recorder.close()
    else:
        with RecordingClient(
            "https://example.invalid", default_headers={"Authorization": f"Bearer {KEY}"}
        ) as client:
            client._client.close()
            client._client = httpx.Client(
                transport=httpx.MockTransport(lambda request: httpx.Response(401, json={}))
            )
            if mode == "stream":
                exchange, _ = client.stream("/v1/chat/completions", json_body={}, headers=headers)
            else:
                exchange = client.post("/v1/chat/completions", json_body={}, headers=headers)
    assert exchange.request_auth == [{"header": "authorization", "kind": "replacement_basic"}]
    assert synthetic not in json.dumps(exchange.to_dict())
    assert KEY not in json.dumps(exchange.to_dict())


def test_another_ingress_registered_key_is_not_replayed_as_this_ingress_key():
    foreign = "another-ingress-credential"
    redact.install_secrets([foreign])
    exchange = exchange_with({"Authorization": f"Bearer {foreign}"})
    assert exchange.request_auth == [{"header": "authorization", "kind": "replacement_bearer"}]
    assert "LLMDV_REPLAY_KEY" not in to_curl(exchange, AUTH)


def test_sdk_capture_knows_its_configured_credential():
    client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    )
    recorder = SdkRecorder(client, auth_headers={"Authorization": f"Bearer {KEY}"})
    try:
        client.post("https://example.invalid", headers={"Authorization": f"Bearer {KEY}"})
        assert recorder.last.request_auth == [{"header": "authorization", "kind": "profile_bearer"}]
    finally:
        recorder.close()
