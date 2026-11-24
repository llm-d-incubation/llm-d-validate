"""Ingresses: how a request reaches a layer, and what the layer's responses reveal.

A product needs no code here: `kind` is a position, and what any product's responses reveal is
read from the one table in `signals.py`, at every layer.
"""

from .base import ServedBy, client_for, served_by, version_signals

__all__ = ["ServedBy", "client_for", "served_by", "version_signals"]
