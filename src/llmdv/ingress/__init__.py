"""Ingress adapters: one class, and a table of what each kind reveals about itself.

Adding an ingress kind is one `IngressKind` member and one row in `ADAPTERS`.
"""

from .base import ADAPTERS, IngressAdapter, ServedBy, adapter_for, client_for

__all__ = ["ADAPTERS", "IngressAdapter", "ServedBy", "adapter_for", "client_for"]
