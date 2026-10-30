from __future__ import annotations

import json
import re
from typing import Any

_SECRET_KEY_PARTS = (
    "api_key",
    "apikey",
    "token",
    "secret",
    "password",
    "authorization",
    "credential",
    "private_key",
)
_INLINE_CREDENTIAL = re.compile(
    r"(?i)(\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|secret|password|"
    r"private[_-]?key)\s*[:=]\s*)"
    r"([^\s,;]+)"
)
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+=*")
_URL_CREDENTIAL = re.compile(r"(?i)([?&](?:token|key|secret|password|api_key)=)[^&#\s]+")
_TOKEN_SHAPE = re.compile(
    r"\b(?:sk|rk|pk|ghp|gho|github_pat|xox[baprs])-[A-Za-z0-9_-]{12,}\b|"
    r"\b(?:ghp|gho|github_pat)_[A-Za-z0-9_]{12,}\b|"
    r"\bAKIA[0-9A-Z]{16}\b"
)
_PRIVATE_KEY_BLOCK = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
    re.DOTALL,
)


class Redactor:
    """Clean public JSON copies without changing execution inputs."""

    def __init__(self, sensitive_values: tuple[str, ...] = ()):
        self.sensitive_values: tuple[str, ...] = ()
        self.add_sensitive_values(sensitive_values)

    def add_sensitive_values(self, values: tuple[str, ...]) -> None:
        self.sensitive_values = tuple(
            sorted(
                set(self.sensitive_values) | {value for value in values if value},
                key=len,
                reverse=True,
            )
        )

    def exception(self, exc: BaseException, action: str) -> str:
        return f"{action} ({type(exc).__name__}); raw diagnostic withheld."

    def clean(self, value: Any, key: str = "") -> Any:
        normalized = re.sub(r"(?<=[a-z])(?=[A-Z])", "_", key).lower().replace("-", "_")
        if any(
            normalized == part or normalized.endswith("_" + part) for part in _SECRET_KEY_PARTS
        ) or normalized in {"credentials", "headers"}:
            return "[redacted]"
        if isinstance(value, dict):
            return {
                self.clean(str(name)): self.clean(item, str(name)) for name, item in value.items()
            }
        if isinstance(value, list | tuple):
            return [self.clean(item) for item in value]
        if isinstance(value, str):
            text = value
            for secret in self.sensitive_values:
                text = text.replace(secret, "[redacted]")
            text = _BEARER.sub("Bearer [redacted]", text)
            text = _INLINE_CREDENTIAL.sub(r"\1[redacted]", text)
            text = _URL_CREDENTIAL.sub(r"\1[redacted]", text)
            text = _TOKEN_SHAPE.sub("[redacted]", text)
            text = _PRIVATE_KEY_BLOCK.sub("[redacted]", text)
            if text.startswith(("{", "[")) and text.endswith(("}", "]")):
                try:
                    parsed = json.loads(text)
                except json.JSONDecodeError:
                    return text
                cleaned = self.clean(parsed)
                if cleaned != parsed:
                    return json.dumps(
                        cleaned, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                    )
            return text
        if value is None or isinstance(value, bool | int | float):
            return value
        raise TypeError(f"session event contains a non-JSON value: {type(value).__name__}")
