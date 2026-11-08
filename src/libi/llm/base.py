from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Literal, Protocol

from libi.llm.pricing import CostBreakdown, ModelPriceProfile
from libi.types import Message, Usage


class ProviderRequestError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        kind: Literal["http", "timeout", "connection", "protocol"],
        status_code: int | None = None,
        code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.details: dict[str, str | int] = {"kind": kind}
        if status_code is not None:
            self.details["status_code"] = status_code
        if code is not None:
            self.details["code"] = code


class LlmClient(Protocol):
    model_name: str
    provider_name: str
    max_context_window: int
    price_profile: ModelPriceProfile | None

    def chat(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]],
        *,
        system_prompt: str,
    ) -> AsyncIterator[dict[str, Any]]: ...

    def calculate_cost(
        self,
        usage: Usage | dict[str, Any],
        *,
        currency: str = "usd",
    ) -> CostBreakdown: ...
