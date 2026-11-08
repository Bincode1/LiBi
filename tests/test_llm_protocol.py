from __future__ import annotations

import asyncio

import httpx
import pytest

from libi.config import LlmConfig
from libi.context import CompactionRequest
from libi.llm import create_llm_client
from libi.types import Message


@pytest.fixture
def client():
    return create_llm_client(LlmConfig(api_key="synthetic-provider-credential"))


async def collect(client):
    return [
        event
        async for event in client.chat(
            [Message(role="user", content="hello")], [], system_prompt="system"
        )
    ]


def test_chat_stream_preserves_reasoning_tool_calls_finish_and_usage(client, provider_stream):
    batches, requests = provider_stream
    tool = {"index": 0, "id": "call", "function": {"name": "read_file", "arguments": "{}"}}
    batches.append(
        [
            {"choices": [{"delta": {"reasoning_content": "thinking", "content": "text"}}]},
            {"choices": [{"delta": {"tool_calls": [tool]}, "finish_reason": "tool_calls"}]},
            {"choices": [], "usage": {"prompt_tokens": 8, "completion_tokens": 2}},
        ]
    )
    events = asyncio.run(collect(client))
    assert [event["type"] for event in events] == [
        "message_start",
        "thinking_delta",
        "text_delta",
        "tool_call_delta",
        "message_end",
        "usage",
    ]
    assert events[3]["tool_call"] == tool
    assert events[4]["stop_reason"] == "tool_use"
    assert events[5]["usage"]["total_tokens"] == 10
    assert requests[0]["messages"] == [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "hello"},
    ]


@pytest.mark.parametrize("status", [401, 429, 503])
def test_provider_http_errors_are_explicit_and_hide_response_content(client, monkeypatch, status):
    async def respond(_transport, _request):
        return httpx.Response(status, text="synthetic-private-response")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", respond)
    events = asyncio.run(collect(client))
    attempts = [
        event["diagnostic"] for event in events if event["type"] == "provider_attempt_failed"
    ]
    expected_attempts = [1, 2] if status == 503 else [1]
    assert [attempt["attempt"] for attempt in attempts] == expected_attempts
    assert [attempt["retrying"] for attempt in attempts] == (
        [True, False] if status == 503 else [False]
    )
    assert all(attempt["provider_error"]["status_code"] == status for attempt in attempts)
    assert "synthetic-private" not in str(attempts)
    assert f"HTTP {status}" in str(events[-1]["error"])
    assert "synthetic-private" not in str(events[-1]["error"])
    assert client.api_key not in str(events[-1]["error"])


def test_slow_first_provider_failure_does_not_start_another_request(client, monkeypatch):
    elapsed = 0.0
    calls = 0

    async def respond(_transport, _request):
        nonlocal elapsed, calls
        calls += 1
        elapsed += 15
        return httpx.Response(500)

    monkeypatch.setattr("libi.llm.openai_compatible.monotonic", lambda: elapsed)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", respond)
    events = asyncio.run(collect(client))
    assert calls == 1
    assert events[-2]["diagnostic"]["retrying"] is False
    assert events[-2]["diagnostic"]["retry_skipped"] == "budget_exhausted"
    assert events[-1]["error"].details == {"kind": "http", "status_code": 500}


def test_retry_waiting_for_response_headers_uses_remaining_budget(client, monkeypatch):
    calls = 0

    async def respond(_transport, _request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(503)
        await asyncio.Event().wait()
        pytest.fail("The retry budget should have canceled this attempt")

    monkeypatch.setattr("libi.llm.openai_compatible._PROVIDER_RETRY_DELAY", 0)
    monkeypatch.setattr("libi.llm.openai_compatible._PROVIDER_RETRY_WINDOW", 0.5, raising=False)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", respond)

    async def run():
        async with asyncio.timeout(2):
            return await collect(client)

    events = asyncio.run(run())
    assert calls == 2
    assert events[-2]["diagnostic"]["stage"] == "response_headers"
    assert events[-1]["error"].details == {"kind": "timeout", "code": "retry_budget_exhausted"}


@pytest.mark.parametrize(
    ("exception", "message", "recovery"),
    [
        (httpx.ReadTimeout, "timed out after 120s", "retry"),
        (httpx.ConnectError, "Could not connect to deepseek", "VPN/proxy"),
    ],
)
def test_provider_network_errors_return_actionable_error_events(
    client, monkeypatch, exception, message, recovery
):
    async def fail(_transport, request):
        raise exception("synthetic-private-network-detail", request=request)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", fail)
    events = asyncio.run(collect(client))
    assert [event["type"] for event in events] == [
        "message_start",
        "provider_attempt_failed",
        "error",
    ]
    diagnostic = events[1]["diagnostic"]
    assert diagnostic["error_type"] == exception.__name__
    assert diagnostic["stage"] == (
        "connect" if exception is httpx.ConnectError else "response_headers"
    )
    error = str(events[-1]["error"])
    assert message in error and recovery in error
    assert "synthetic-private" not in error
    assert client.api_key not in error


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ("", "incomplete_stream"),
        ('data: {"choices":[{"delta":{"content":"partial"}}]}\n\n', "incomplete_stream"),
        ('data: {"choices":[null]}\n\n', "invalid_sse_event"),
        (
            'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
            'data: {"error":{"message":"synthetic-private-response"}}\n\n'
            "data: [DONE]\n\n",
            "provider_error_frame",
        ),
    ],
)
def test_invalid_provider_stream_is_recorded_as_protocol_failure(client, monkeypatch, body, code):
    async def respond(_transport, _request):
        return httpx.Response(200, headers={"x-request-id": "incomplete-stream"}, text=body)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", respond)
    events = asyncio.run(collect(client))
    assert [event["type"] for event in events[-2:]] == ["provider_attempt_failed", "error"]
    diagnostic = events[-2]["diagnostic"]
    assert diagnostic["stage"] == "response_stream"
    assert diagnostic["provider_error"] == {"kind": "protocol", "code": code}
    assert diagnostic["upstream_request_id"] == "incomplete-stream"
    assert diagnostic["retrying"] is False
    assert "synthetic-private-response" not in str(events)


def test_missing_provider_key_never_starts_http_request(client, monkeypatch):
    client.api_key = ""

    async def unexpected(_transport, _request):
        pytest.fail("missing credentials reached HTTP transport")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", unexpected)
    events = asyncio.run(collect(client))
    assert len(events) == 1 and events[0]["type"] == "error"


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        ("stop", "end_turn"),
        ("tool_calls", "tool_use"),
        ("length", "max_tokens"),
        ("content_filter", "stop_sequence"),
        ("unknown", "end_turn"),
    ],
)
def test_provider_finish_reasons(client, reason, expected):
    async def run():
        return [
            event
            async for event in client._parse_chunk(
                {"choices": [{"delta": {}, "finish_reason": reason}]}
            )
        ]

    assert asyncio.run(run()) == [{"type": "message_end", "stop_reason": expected}]


def test_compaction_request_uses_isolated_route_without_tools(client, provider_stream):
    batches, requests = provider_stream
    batches.append(
        [
            {"choices": [{"delta": {"content": "summary"}, "finish_reason": "stop"}]},
            {"choices": [], "usage": {"prompt_tokens": 20, "completion_tokens": 5}},
        ]
    )
    request = CompactionRequest(
        purpose="compaction",
        provider="test",
        model="summary-model",
        system_prompt="summarize",
        messages=(Message(role="user", content="transcript"),),
        tools=(),
        max_tokens=100,
        temperature=0,
    )
    response = asyncio.run(client.complete(request))
    assert response.text == "summary" and response.usage.total_tokens == 25
    assert response.model == "summary-model"
    payload = requests[0]
    assert payload["model"] == "summary-model" and payload["max_tokens"] == 100
    assert payload["temperature"] == 0
    assert "tools" not in payload
    assert client.model != "summary-model"


def test_format_messages_keeps_tool_call_result_pairs(client):
    call = {"id": "call", "type": "function", "function": {"name": "read", "arguments": "{}"}}
    payload = client._build_payload(
        [
            Message(role="user", content="request"),
            Message(role="assistant", content="", tool_calls=[call]),
            Message(role="tool", content="result", tool_call_id="call"),
        ],
        [{"type": "function", "function": {"name": "read"}}],
        system_prompt="system",
    )
    assert payload["messages"][2]["tool_calls"] == [call]
    assert payload["messages"][3] == {"role": "tool", "tool_call_id": "call", "content": "result"}
    assert payload["tool_choice"] == "auto"
