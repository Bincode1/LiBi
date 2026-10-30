from __future__ import annotations

import uuid
from copy import deepcopy

from paicli.session.models import ContextView, LlmRequest
from paicli.types import Message


class RequestBuilder:
    """Create a request snapshot without mutating its source ContextView."""

    def build(
        self,
        view: ContextView,
        *,
        model: str,
        provider: str,
        max_tokens: int,
        temperature: float,
    ) -> LlmRequest:
        if max_tokens <= 0:
            raise ValueError("max_tokens must be positive")
        messages = tuple(
            Message(
                role=message.role,
                content=deepcopy(message.content),
                name=message.name,
                tool_call_id=message.tool_call_id,
                tool_calls=deepcopy(message.tool_calls),
            )
            for message in view.messages
        )
        tools = tuple(deepcopy(tool) for tool in view.tools)
        return LlmRequest(
            request_id=f"req_{uuid.uuid4().hex}",
            session_id=view.session_id,
            turn_id=view.turn_id,
            step_id=view.step_id,
            model=model,
            provider=provider,
            system_prompt=view.system_prompt,
            messages=messages,
            tools=tools,
            max_tokens=max_tokens,
            temperature=temperature,
            metadata={
                "projection_version": view.projection_version,
                "source_sequences": list(view.source_sequences),
                "compaction_id": view.compaction_id,
                "through_sequence": view.through_sequence,
            },
            context_hash=view.context_hash,
        )
