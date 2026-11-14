"""The effective declarations shown beside an ingress in profile and run output."""

from __future__ import annotations

import json
from dataclasses import asdict

from ..profile.schema import Capabilities, Model


def contract_lines(model: Model, capabilities: Capabilities) -> tuple[str, ...]:
    reasoning = asdict(model.reasoning)
    reasoning["field"] = reasoning.pop("field_name")
    return (
        f"model={model.public_id}  aliases={json.dumps(model.aliases)}"
        f"  max_model_len={model.max_model_len}  answer_max_tokens={model.answer_max_tokens}",
        f"reasoning={json.dumps(reasoning)}  tools={json.dumps(asdict(model.tools))}",
        f"capabilities={json.dumps(asdict(capabilities))}",
    )
