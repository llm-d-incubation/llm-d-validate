"""Surface registry.

Concrete surfaces register themselves at import time, so this package must import each one
for `base.implemented()` to be accurate. Without it the registry is empty and every declared
surface is reported as not implemented -- which looks exactly like a coverage gap rather than
a missing import.

Adding a surface means one module plus one line here.
"""

from . import (  # noqa: F401 -- register the surfaces
    chat_completions,
    completions,
    messages,
    render,
    responses,
)
from .base import (
    KNOWN_SURFACES,
    SURFACE_PATHS,
    Completion,
    ErrorEnvelope,
    Surface,
    Usage,
    get,
    implemented,
    register,
)

__all__ = [
    "KNOWN_SURFACES",
    "SURFACE_PATHS",
    "Completion",
    "ErrorEnvelope",
    "Surface",
    "Usage",
    "get",
    "implemented",
    "register",
]
