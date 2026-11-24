"""Ingress adapters: one class, and a table of what the layer at each position reveals.

A product needs no code here: `kind` is a position, and a product in it reads the headers of
its position's row, recording only those it sends.
"""

from .base import ADAPTERS, IngressAdapter, ServedBy, adapter_for, client_for

__all__ = ["ADAPTERS", "IngressAdapter", "ServedBy", "adapter_for", "client_for"]
