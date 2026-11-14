"""Each declared binary version is a separate result/evidence variant."""

from __future__ import annotations

import pytest


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    if "client_binary" not in metafunc.fixturenames:
        return
    profile = getattr(metafunc.config, "_llmdv_profile", None)
    selected = metafunc.config.getoption("--llmdv-client") or []
    values = [
        (f"{client.id}@{binary.version}", binary)
        for client in (profile.clients if profile else ())
        if not selected or client.id in selected
        for binary in client.binaries
    ]
    if not values:
        values = [
            pytest.param(
                None, marks=pytest.mark.skip(reason="[undeclared] no pinned real-client binaries")
            )
        ]
    metafunc.parametrize(
        "client_binary",
        values,
        ids=lambda value: value[0] if isinstance(value, tuple) else "undeclared",
    )
