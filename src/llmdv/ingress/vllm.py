"""Direct vLLM: the engine's own OpenAI-compatible server.

Nothing sits in front, so a failure here is the backend, the model template, or the fixture.
vLLM reveals no backend identity in its response headers -- verified against v0.30.0, which
returns only `server: uvicorn` and `x-went-into-resp-headers` -- so `backend_pod` stays None
and the port-forward target recorded in the bundle is what identifies the replica.
"""

from __future__ import annotations

from ..profile.schema import IngressKind
from .base import IngressAdapter, register


class VllmAdapter(IngressAdapter):
    kind = IngressKind.VLLM
    backend_headers = ()


VLLM = register(VllmAdapter())
