"""Collection-time parametrisation for the contract suite.

The header cases are generated from the profile rather than written out, so declaring a new
header in a profile creates its verification case with no code change. That is the point of
declare-then-verify: the declaration is the test input.
"""

from __future__ import annotations

import pytest

from ...profile.schema import HeaderCase


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    if "declared_header" not in metafunc.fixturenames:
        return
    profile = getattr(metafunc.config, "_llmdv_profile", None)
    if profile is None:
        metafunc.parametrize("declared_header", [], ids=[])
        return

    selected = metafunc.config.getoption("--llmdv-ingress") or list(profile.ingress_ids)
    cases, ids = [], []
    for header, per_ingress in sorted(profile.headers.items()):
        for ingress_id, disposition in sorted(per_ingress.items()):
            if ingress_id not in selected:
                continue
            cases.append(HeaderCase(header, ingress_id, disposition))
            ids.append(f"{header}@{ingress_id}")

    metafunc.parametrize("declared_header", cases, ids=ids)
