"""One schema for both transport modes; its marker never appears in the prompt."""

from __future__ import annotations

import uuid
from typing import Any


def schema_probe() -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "marker": {"type": "string", "enum": [uuid.uuid4().hex[:12]]},
            "count": {"type": "integer"},
        },
        "required": ["marker", "count"],
        "additionalProperties": False,
    }
