"""SDK fixtures must use the credentials the plugin resolved and registered for redaction."""

from __future__ import annotations

import anthropic
import pytest

from llmdv.profile.schema import Auth, AuthType, Capabilities, Ingress, IngressKind, Model
from llmdv.suites import conftest as fixtures
from llmdv.surfaces import get
from tests.fakes import FakeFleet


@pytest.mark.parametrize("sdk", ["openai", "anthropic"])
@pytest.mark.parametrize("secret", [None, "resolved-profile-secret"])
def test_sdk_credentials_come_from_the_plugin_without_disk_discovery(
    request, monkeypatch, sdk, secret
):
    monkeypatch.setattr(request.config, "_llmdv_secrets", {"fake": secret}, raising=False)
    monkeypatch.setattr(request.config, "_llmdv_run_id", "sdk-run", raising=False)
    # A changed environment value must not bypass the plugin's registered secret snapshot.
    monkeypatch.setenv("LLMDV_SDK_KEY", "changed-after-secret-resolution")
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY"):
        monkeypatch.delenv(name, raising=False)

    def unexpected_discovery(**kwargs):
        pytest.fail("SDK attempted to discover credentials outside the profile")

    monkeypatch.setattr(anthropic._client, "default_credentials", unexpected_discovery)
    recorders = []
    monkeypatch.setattr("llmdv.plugin.register_client", lambda _, __, http: recorders.append(http))
    expected = f"Bearer {secret or 'not-required'}"
    with FakeFleet(auth_header=("Authorization", expected)) as fleet:
        ingress = Ingress(
            id="fake",
            kind=IngressKind.VLLM,
            base_url=fleet.base_url,
            model=Model(public_id=fleet.model),
            capabilities=Capabilities(),
            auth=Auth(type=AuthType.BEARER, key_env="LLMDV_SDK_KEY"),
        )
        surface = get("messages" if sdk == "anthropic" else "chat_completions")
        fixture = (
            fixtures.anthropic_client.__wrapped__(request, ingress, surface, monkeypatch)
            if sdk == "anthropic"
            else fixtures.openai_client.__wrapped__(request, ingress)
        )
        client = next(fixture)
        try:
            if sdk == "anthropic":
                client.messages.create(
                    model=fleet.model,
                    max_tokens=16,
                    messages=[{"role": "user", "content": "ok"}],
                )
            else:
                client.chat.completions.create(**surface.minimal_body(fleet.model))
        finally:
            fixture.close()

    ((_, _, headers),) = fleet.seen
    headers = {name.lower(): value for name, value in headers.items()}
    assert headers["authorization"] == expected
    assert headers["x-llmdv-run-id"] == "sdk-run"
    assert headers["x-llmdv-case-id"] == request.node.nodeid
    assert len(recorders[0].exchanges) == 1
    assert recorders[0].last.status == 200
    assert recorders[0].client.is_closed
