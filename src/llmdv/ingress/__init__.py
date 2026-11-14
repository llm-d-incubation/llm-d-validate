"""Ingress registry.

Each adapter registers at import time, so this package imports all three. Adding an ingress
kind is one module, one line here, and one new `IngressKind` member.
"""

from . import litellm, router, vllm  # noqa: F401 -- imported for registration side effects
from .base import IngressAdapter, ServedBy, adapter_for, client_for, register

__all__ = ["IngressAdapter", "ServedBy", "adapter_for", "client_for", "register"]
