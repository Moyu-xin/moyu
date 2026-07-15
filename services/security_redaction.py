from __future__ import annotations

import re
from typing import Any


_SENSITIVE_KEY_PARTS = (
    "access_token",
    "refresh_token",
    "id_token",
    "session_token",
    "authorization",
    "password",
    "secret",
    "cookie",
    "code_verifier",
)
_SENSITIVE_EXACT_KEYS = {"code", "session_id"}

_TOKEN_PATTERNS = (
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+\-/]+=*"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}(?:\.[A-Za-z0-9_-]{8,})?\b"),
    re.compile(
        r"(?i)(?<![A-Za-z0-9_])(access_token|refresh_token|id_token|session_token|password|secret|code|code_verifier|authorization)"
        r"(\s*[=:]\s*|%3[dD])([^&\s,}\]]+)"
    ),
)


def redact_text(value: Any) -> str:
    text = str(value or "")
    text = _TOKEN_PATTERNS[0].sub("Bearer [REDACTED]", text)
    text = _TOKEN_PATTERNS[1].sub("[REDACTED_JWT]", text)
    text = _TOKEN_PATTERNS[2].sub(lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", text)
    return text


def redact_value(value: Any) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            lowered = str(key).lower()
            is_sensitive = lowered in _SENSITIVE_EXACT_KEYS or any(part in lowered for part in _SENSITIVE_KEY_PARTS)
            result[str(key)] = "[REDACTED]" if is_sensitive else redact_value(item)
        return result
    if isinstance(value, list):
        return [redact_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_value(item) for item in value)
    if isinstance(value, str):
        return redact_text(value)
    return value
