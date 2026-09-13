from __future__ import annotations

import re
from typing import Any

_SENSITIVE_KEYS = {
    "password",
    "pass",
    "secret",
    "token",
    "apiKey",
    "api_key",
    "apikey",
    "pin",
    "otp",
    "tpassword",
    "cvv",
    "ssn",
    "verification_code",
    "card_number",
    "account_number",
    "routing_number",
    "ssn_number",
}
_PATTERNS = [
    re.compile(r"\b\d{4}[- ]?\d{4}[- ]?\d{4}[- ]?\d{4}\b"),
    re.compile(r"\b\d{16}\b"),
]


def redact(value: Any, sensitive_params: set[str] | None = None) -> Any:
    sensitive_params = sensitive_params or set()
    if isinstance(value, dict):
        return {
            str(k): (
                "***"
                if _is_sensitive_key(str(k)) or str(k) in sensitive_params
                else redact(v, sensitive_params)
            )
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact(v, sensitive_params) for v in value]
    if isinstance(value, tuple):
        return tuple(redact(v, sensitive_params) for v in value)
    if isinstance(value, str):
        out = value
        for pattern in _PATTERNS:
            out = pattern.sub("***", out)
        return out
    return value


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return lowered in _SENSITIVE_KEYS or "pass" in lowered or "token" in lowered


def log_redacted(event: dict[str, Any]) -> dict[str, Any]:
    return dict(redact(event))