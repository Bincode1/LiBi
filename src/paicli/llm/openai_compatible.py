from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from dataclasses import dataclass, replace
from time import monotonic
from typing import Any

import httpx

from paicli.llm.base import ProviderRequestError
from paicli.llm.pricing import CostBreakdown, ModelPriceProfile, calculate_cost
from paicli.types import Message, Usage

_RETRYABLE_HTTP_STATUSES = {500, 502, 503, 504}
_PROVIDER_RETRY_DELAY = 0.5
_PROVIDER_RETRY_WINDOW = 5.0


@dataclass(slots=True)
class OpenAICompatibleClient:
    provider_name: str
    model: str
    api_key: str
    base_url: str
    max_tokens: int = 8192
    temperature: float = 0.7
    thinking_enabled: bool = False
    timeout: float = 120.0
    max_context_window: int = 128_000
    prompt_cache: bool = False
    price_profile: ModelPriceProfile | None = None

    @property
    def model_name(self) -> str:
        return self.model

    @property
    def supports_images(self) -> bool:
        model = self.model.lower()
        provider = self.provider_name.lower()
        return any(marker in model for marker in ("vision", "image", "5v", "vl")) or (
            provider in {"glm", "zhipu"} and "5v" in model
        )

    def calculate_cost(
        self,
        usage: Usage | dict[str, Any],
        *,
        currency: str = "usd",
    ) -> CostBreakdown:
        if self.price_profile is None:
            raise ValueError(
                f'No price profile is configured for model "{self.model}". '
                "Set llm.prices in PaiCLI config."
            )
        return calculate_cost(usage, self.price_profile, currency=currency)

    async def chat(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]],
        *,
        system_prompt: str,
        retry_transient_errors: bool = True,
    ) -> AsyncIterator[dict[str, Any]]:
        if not self.api_key:
            yield {
                "type": "error",
                "error": RuntimeError(
                    "PAICLI_API_KEY is not configured. Set it in env, ~/.paicli/config.json, "
                    "or project .paicli/config.json."
                ),
            }
            return

        payload = self._build_payload(messages, tools, system_prompt=system_prompt)

        headers = {
            "authorization": f"Bearer {self.api_key}",
            "content-type": "application/json",
            "user-agent": "PaiCLI-Python/0.1.0",
        }
        url = self.base_url.rstrip("/") + "/chat/completions"

        yield {"type": "message_start", "model": self.model}
        async with httpx.AsyncClient(timeout=self.timeout, http2=False) as client:
            retry_deadline = monotonic() + min(self.timeout, _PROVIDER_RETRY_WINDOW)
            for attempt in (1, 2) if retry_transient_errors else (1,):
                started = monotonic()
                stage = "response_headers"
                upstream_id = None
                try:
                    async with AsyncExitStack() as stack:
                        # Bound the automatic retry through headers, then stream normally.
                        remaining = max(0.0, retry_deadline - started) if attempt == 2 else None
                        if remaining == 0:
                            raise TimeoutError
                        async with asyncio.timeout(remaining):
                            response = await stack.enter_async_context(
                                client.stream("POST", url, headers=headers, json=payload)
                            )
                        upstream_id = response.headers.get("x-request-id") or response.headers.get(
                            "request-id"
                        )
                        response.raise_for_status()
                        stage = "response_stream"
                        async for parsed in self._stream_completion(response):
                            yield parsed
                    return
                except (httpx.HTTPError, ProviderRequestError, TimeoutError) as exc:
                    error = self._classify_failure(exc)
                    if isinstance(exc, httpx.ConnectError | httpx.ConnectTimeout):
                        stage = "connect"
                    retryable = (
                        retry_transient_errors
                        and attempt == 1
                        and error.details.get("status_code") in _RETRYABLE_HTTP_STATUSES
                    )
                    failed_at = monotonic()
                    retrying = retryable and failed_at + _PROVIDER_RETRY_DELAY < retry_deadline
                    diagnostic: dict[str, Any] = {
                        "attempt": attempt,
                        "stage": stage,
                        "elapsed_ms": round((failed_at - started) * 1000),
                        "error_type": type(exc).__name__,
                        "retrying": retrying,
                        "provider_error": error.details,
                    }
                    if retryable and not retrying:
                        diagnostic["retry_skipped"] = "budget_exhausted"
                    if (
                        upstream_id
                        and self.api_key not in upstream_id
                        and re.fullmatch(r"[a-zA-Z0-9._:-]{1,128}", upstream_id)
                    ):
                        diagnostic["upstream_request_id"] = upstream_id
                yield {"type": "provider_attempt_failed", "diagnostic": diagnostic}
                if not retrying:
                    yield {"type": "error", "error": error}
                    return
                await asyncio.sleep(_PROVIDER_RETRY_DELAY)

    def _classify_failure(
        self, exc: httpx.HTTPError | ProviderRequestError | TimeoutError
    ) -> ProviderRequestError:
        if isinstance(exc, ProviderRequestError):
            return exc
        if isinstance(exc, TimeoutError):
            return ProviderRequestError(
                "Automatic retry timed out waiting for the provider response headers.",
                kind="timeout",
                code="retry_budget_exhausted",
            )
        if isinstance(exc, httpx.HTTPStatusError):
            status = exc.response.status_code
            return ProviderRequestError(
                f"{self.provider_name} API returned HTTP {status}. "
                "Check the API key, model access, account balance, and provider status.",
                kind="http",
                status_code=status,
            )
        if isinstance(exc, httpx.TimeoutException):
            return ProviderRequestError(
                f"{self.provider_name} request timed out after {self.timeout:g}s. "
                "Check the network and retry.",
                kind="timeout",
            )
        return ProviderRequestError(
            f"Could not connect to {self.provider_name}. "
            "Check the network, VPN/proxy, and provider status, then retry.",
            kind="connection",
        )

    async def _stream_completion(self, response: httpx.Response) -> AsyncIterator[dict[str, Any]]:
        finished = False
        has_output = False
        async for data in _iter_sse(response):
            if data.strip() == "[DONE]":
                finished = True
                break
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                raise ProviderRequestError(
                    "Provider returned invalid JSON in an SSE event",
                    kind="protocol",
                    code="invalid_sse_json",
                ) from None
            if not isinstance(chunk, dict):
                raise ProviderRequestError(
                    "Provider returned an invalid SSE event",
                    kind="protocol",
                    code="invalid_sse_event",
                )
            if chunk.get("error") is not None:
                raise ProviderRequestError(
                    "Provider reported an error in the response stream",
                    kind="protocol",
                    code="provider_error_frame",
                )
            try:
                async for event in self._parse_chunk(chunk):
                    kind = event["type"]
                    finished |= kind == "message_end"
                    has_output |= kind in {"text_delta", "thinking_delta", "tool_call_delta"}
                    yield event
            except (AttributeError, IndexError, KeyError, TypeError, ValueError):
                raise ProviderRequestError(
                    "Provider returned an invalid SSE event",
                    kind="protocol",
                    code="invalid_sse_event",
                ) from None
        if not finished or not has_output:
            raise ProviderRequestError(
                "Provider returned an empty or incomplete response stream",
                kind="protocol",
                code="incomplete_stream",
            )

    async def complete(self, request: Any) -> Any:
        """Execute an isolated compaction request with explicit route parameters."""
        from paicli.context.compaction import CompactionResponse

        if getattr(request, "purpose", None) != "compaction":
            raise ValueError("OpenAICompatibleClient.complete only accepts compaction requests")
        client = replace(
            self,
            provider_name=str(request.provider or self.provider_name),
            model=str(request.model or self.model),
            max_tokens=int(request.max_tokens),
            temperature=float(request.temperature),
        )
        text: list[str] = []
        usage = None
        finish_reason = None
        async for event in client.chat(
            list(request.messages), [], system_prompt=request.system_prompt
        ):
            kind = event.get("type")
            if kind == "text_delta":
                text.append(str(event.get("text") or ""))
            elif kind == "usage":
                usage = Usage.from_mapping(event.get("usage") or {})
            elif kind == "message_end":
                finish_reason = str(event.get("stop_reason") or "end_turn")
            elif kind == "tool_call_delta":
                finish_reason = "tool_use"
            elif kind == "provider_attempt_failed" and request.on_provider_attempt_failed:
                await request.on_provider_attempt_failed(event["diagnostic"])
            elif kind == "error":
                raise event["error"]
        return CompactionResponse(
            text="".join(text),
            finish_reason=finish_reason,
            usage=usage,
            provider=client.provider_name,
            model=client.model,
            parameters={
                "max_tokens": client.max_tokens,
                "temperature": client.temperature,
                "tools": [],
            },
        )

    def _build_payload(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]],
        *,
        system_prompt: str,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": self._format_messages(messages, system_prompt),
            "stream": True,
            "stream_options": {"include_usage": True},
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
        }
        if self.provider_name.lower() == "deepseek":
            payload["thinking"] = {"type": "enabled" if self.thinking_enabled else "disabled"}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        return payload

    def _format_messages(self, messages: list[Message], system_prompt: str) -> list[dict[str, Any]]:
        formatted: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
        for message in messages:
            if message.role == "tool":
                formatted.append(
                    {
                        "role": "tool",
                        "tool_call_id": message.tool_call_id or "",
                        "content": str(message.content),
                    }
                )
            elif message.role == "assistant":
                item: dict[str, Any] = {"role": "assistant", "content": message.content or ""}
                if message.tool_calls:
                    item["tool_calls"] = message.tool_calls
                formatted.append(item)
            else:
                formatted.append(
                    {"role": message.role, "content": self._format_content(message.content)}
                )
        return formatted

    def _format_content(self, content: str | list[dict[str, Any]]) -> str | list[dict[str, Any]]:
        if isinstance(content, str):
            return content
        if self.supports_images:
            cleaned = []
            for part in content:
                item = {key: value for key, value in part.items() if key != "metadata"}
                cleaned.append(item)
            return cleaned
        text_parts = []
        for part in content:
            if part.get("type") == "text":
                text_parts.append(str(part.get("text") or ""))
            elif part.get("type") == "image_url":
                metadata = part.get("metadata") or {}
                source = metadata.get("source", "remote image")
                width = metadata.get("width", "?")
                height = metadata.get("height", "?")
                text_parts.append(f"[Image omitted: {source}, {width}x{height}]")
        return "\n".join(text_parts)

    async def list_models(self) -> list[str]:
        """Fetch available model IDs from the API's /v1/models endpoint.

        Returns an empty list if the API key is missing or the request fails.
        """
        if not self.api_key:
            return []
        headers = {
            "authorization": f"Bearer {self.api_key}",
            "user-agent": "PaiCLI-Python/0.1.0",
        }
        url = self.base_url.rstrip("/") + "/models"
        async with httpx.AsyncClient(timeout=15.0, http2=False) as client:
            try:
                response = await client.get(url, headers=headers)
                response.raise_for_status()
                data = response.json()
                return sorted(item["id"] for item in data.get("data", []))
            except Exception:
                return []

    async def _parse_chunk(self, chunk: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
        choices = chunk.get("choices") or []
        if choices:
            choice = choices[0]
            delta = choice.get("delta") or {}

            reasoning = delta.get("reasoning_content")
            if isinstance(reasoning, str) and reasoning:
                yield {"type": "thinking_delta", "thinking": reasoning}

            content = delta.get("content")
            if isinstance(content, str) and content:
                yield {"type": "text_delta", "text": content}

            tool_calls = delta.get("tool_calls") or []
            for tool_call in tool_calls:
                yield {"type": "tool_call_delta", "tool_call": tool_call}

            finish_reason = choice.get("finish_reason")
            if finish_reason:
                yield {
                    "type": "message_end",
                    "stop_reason": _map_finish_reason(str(finish_reason)),
                }

        usage = chunk.get("usage")
        if isinstance(usage, dict):
            yield {"type": "usage", "usage": Usage.from_mapping(usage).to_dict()}


async def _iter_sse(response: httpx.Response) -> AsyncIterator[str]:
    data_lines: list[str] = []
    async for line in response.aiter_lines():
        if line == "":
            if data_lines:
                yield "\n".join(data_lines)
                data_lines.clear()
        elif line == "data":
            data_lines.append("")
        elif line.startswith("data:"):
            value = line[5:]
            data_lines.append(value[1:] if value.startswith(" ") else value)
    if data_lines:
        yield "\n".join(data_lines)


def _map_finish_reason(reason: str) -> str:
    if reason in {"tool_calls", "tool_use"}:
        return "tool_use"
    if reason == "length":
        return "max_tokens"
    if reason == "content_filter":
        return "stop_sequence"
    return "end_turn"
