"""A stand-in for an llm-d guide's router, for testing deploy/llm-d-nightly/smoke-step.sh.

    python fake_router.py serve     the fake fleet on 0.0.0.0:8081; FAKE_DEFECT names one of
                                    FakeFleet's defect switches (for example omit_done)
    python fake_router.py profile   a profile for it, shaped as the guide profiles are: one
                                    `router` ingress that reads its URL from LLMDV_ROUTER_URL

The fake fleet binds 127.0.0.1 for the offline suite; `serve` binds every interface instead, so a
Service can route to it.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tests import transport_fakes  # noqa: E402
from tests.fakes import FakeFleet, profile_yaml  # noqa: E402

PORT = 8081


def serve() -> None:
    base = transport_fakes.QuietServer

    class Bound(base):
        def __init__(self, address, handler):
            super().__init__(("0.0.0.0", PORT), handler)  # noqa: S104 -- a Service routes to it

    transport_fakes.QuietServer = Bound
    defect = os.environ.get("FAKE_DEFECT", "")
    with FakeFleet(
        delay_s=float(os.environ.get("FAKE_DELAY_SECONDS", "0")),
        **({defect: True} if defect else {}),
    ) as fleet:
        sys.stdout.write(f"fake router on {fleet.base_url} (defect: {defect or 'none'})\n")
        sys.stdout.flush()
        while True:
            time.sleep(3600)


def _rename(value, old: str, new: str):
    if isinstance(value, dict):
        return {(new if k == old else k): _rename(v, old, new) for k, v in value.items()}
    if isinstance(value, list):
        return [_rename(v, old, new) for v in value]
    return value


def profile() -> None:
    document = yaml.safe_load(profile_yaml("http://placeholder"))
    old = document["ingresses"][0]["id"]
    document["name"] = "optimized-baseline-fake-router"
    document["headers"] = _rename(document.get("headers") or {}, old, "router")
    for entry in document.get("known_failures") or []:
        if entry.get("ingress") == old:
            entry["ingress"] = "router"
    ingress = document["ingresses"][0]
    ingress.update(id="router", kind="router", base_url_env="LLMDV_ROUTER_URL")
    ingress.pop("base_url", None)
    yaml.safe_dump(document, sys.stdout, sort_keys=False)


if __name__ == "__main__":
    {"serve": serve, "profile": profile}[sys.argv[1]]()
