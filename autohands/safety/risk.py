from __future__ import annotations

import re
from typing import Any, Literal

RISKY_ACTION_PATTERNS = [
    re.compile(r"(?i)(delete|remove|cancel|cancel.*account|close.*account|transfer|wire|pay|payment|withdraw|send.*funds|finalize|submit.*irrevocable)"),
    re.compile(r"(?i)(http[s]?://|www\.)"),
]

RISKY_PARAM_PATTERNS = [
    re.compile(r"(?i)(password|secret|token|pin|cvv|card|account_number|routing_number)"),
]


def classify_risk(
    action: str,
    description: str,
    domain: str = "",
    params: dict[str, Any] | None = None,
) -> Literal["low", "medium", "high"]:
    text = f"{action} {description} {domain}"
    params = params or {}
    for pattern in RISKY_ACTION_PATTERNS:
        if pattern.search(text):
            return "high"
    for pattern in RISKY_PARAM_PATTERNS:
        for key in params:
            if pattern.search(str(key)) or pattern.search(str(params[key])):
                return "high"
    return "low"


def route_decision(
    risk: Literal["low", "medium", "high"],
    policy: dict[str, str],
) -> Literal["allow", "escalate", "block"]:
    default = {"low": "allow", "medium": "allow", "high": "escalate"}[risk]
    return default