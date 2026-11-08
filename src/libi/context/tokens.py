from __future__ import annotations

import json
import math
import re

from libi.types import Message


def estimate_message_tokens(message: Message) -> int:
    if isinstance(message.content, str):
        content_tokens = estimate_text_tokens(message.content)
    else:
        content_tokens = sum(_estimate_part(part) for part in message.content)
    tool_calls = (
        json.dumps(message.tool_calls, ensure_ascii=False, separators=(",", ":"))
        if message.tool_calls
        else ""
    )
    # A small per-message allowance covers role markers and provider serialization.
    return 4 + content_tokens + estimate_text_tokens(tool_calls)


def estimate_text_tokens(text: str) -> int:
    """Conservative dependency-free token estimate for mixed Chinese/code/English text."""

    if not text:
        return 0
    cjk = len(re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]", text))
    non_cjk = len(text) - cjk
    # CJK characters are often close to one token; code and Latin text average several
    # characters per token. Using 3 chars/token leaves room for punctuation-heavy source code.
    return cjk + math.ceil(max(0, non_cjk) / 3)


def fit_text(text: str, max_tokens: int, *, max_bytes: int | None = None) -> str:
    """Take a character-aligned prefix under the byte and estimated token limits."""
    if max_bytes is not None:
        text = text.encode("utf-8", errors="replace")[:max_bytes].decode("utf-8", errors="ignore")
    if estimate_text_tokens(text) <= max_tokens:
        return text
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if estimate_text_tokens(text[:middle]) <= max_tokens:
            low = middle
        else:
            high = middle - 1
    return text[:low]


def _estimate_part(part: dict) -> int:
    if part.get("type") == "image_url":
        return 2048
    if part.get("type") == "attachment":
        return (
            2048
            if str(part.get("media_type", "")).startswith("image/")
            else min(3600, int(part.get("size", 0)))
        )
    return estimate_text_tokens(json.dumps(part, ensure_ascii=False, separators=(",", ":")))
