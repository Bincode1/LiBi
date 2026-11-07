from __future__ import annotations

import asyncio
from contextlib import aclosing
from dataclasses import replace

from paicli.config import PaiCliConfig
from paicli.llm import create_llm_client
from paicli.session.models import MAX_SESSION_TITLE_LENGTH
from paicli.types import Message

_TITLE_PROMPT = "Write a short chat title in the user's language. Return only the title."
_TITLE_TIMEOUT = 8.0


async def generate_session_title(config: PaiCliConfig, message: str) -> str | None:
    client = create_llm_client(
        replace(
            config.llm,
            model=config.session_title.model or config.llm.model,
            thinking_enabled=False,
            temperature=0.2,
            max_tokens=64,
        )
    )
    parts: list[str] = []
    events = client.chat(
        [Message(role="user", content=message[:4000])],
        [],
        system_prompt=_TITLE_PROMPT,
        retry_transient_errors=False,
    )
    async with asyncio.timeout(_TITLE_TIMEOUT), aclosing(events):
        async for event in events:
            if event["type"] == "text_delta":
                parts.append(event["text"])
            elif event["type"] == "error":
                raise event["error"]
    title = "".join(parts).strip().partition("\n")[0].strip(' "\x27`“”')
    return title[:MAX_SESSION_TITLE_LENGTH].rstrip() or None
