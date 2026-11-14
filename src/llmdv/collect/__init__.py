"""Log collection: a layer's own account of what happened, alongside the HTTP evidence."""

from .base import CollectedLog, Collection, LogCollector
from .kubectl import KubectlLogCollector
from .noop import NoopCollector

__all__ = [
    "CollectedLog",
    "Collection",
    "KubectlLogCollector",
    "LogCollector",
    "NoopCollector",
]
