from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from paicli.agent import Agent
from paicli.config import LlmConfig, PaiCliConfig, load_config
from paicli.llm import calculate_cost, create_llm_client, get_builtin_price_profile
from paicli.llm.openai_compatible import OpenAICompatibleClient, _iter_sse
from paicli.tools import ToolRegistry
from paicli.types import Message, Usage


def test_streaming_payload_requests_usage() -> None:
    client = _client()

    payload = client._build_payload(
        [Message(role="user", content="hello")],
        [],
        system_prompt="system",
    )

    assert payload["stream"] is True
    assert payload["stream_options"] == {"include_usage": True}
    assert payload["thinking"] == {"type": "disabled"}


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_sse_handles_line_endings_across_byte_boundaries(newline):
    class ByteStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            data = newline.join(
                [": heartbeat", "", 'data: {"value":', "data: 1}", "", "data: [DONE]", "", ""]
            ).encode("utf-8")
            for byte in data:
                yield bytes([byte])

    async def collect():
        response = httpx.Response(200, stream=ByteStream())
        try:
            return [event async for event in _iter_sse(response)]
        finally:
            await response.aclose()

    assert asyncio.run(collect()) == ['{"value":\n1}', "[DONE]"]


def test_chat_rejects_malformed_sse_json_without_exposing_payload(monkeypatch):
    marker = "synthetic-sensitive-marker"
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, text=f"data: {{invalid {marker}\n\n")
    )
    client_factory = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kwargs: client_factory(transport=transport, **kwargs)
    )
    events = asyncio.run(_collect_chat_events(_client()))
    assert events[-2]["diagnostic"]["provider_error"] == {
        "kind": "protocol",
        "code": "invalid_sse_json",
    }
    assert "invalid JSON" in str(events[-1]["error"])
    assert marker not in str(events)


def test_deepseek_thinking_can_be_enabled() -> None:
    client = OpenAICompatibleClient(
        provider_name="deepseek",
        model="deepseek-flash",
        api_key="key",
        base_url="https://api.deepseek.com/v1",
        thinking_enabled=True,
    )

    payload = client._build_payload(
        [Message(role="user", content="hello")],
        [],
        system_prompt="system",
    )

    assert payload["thinking"] == {"type": "enabled"}


def test_non_deepseek_payload_does_not_send_deepseek_thinking_option() -> None:
    client = OpenAICompatibleClient(
        provider_name="openai-compatible",
        model="private-coder",
        api_key="key",
        base_url="https://llm.example/v1",
    )

    payload = client._build_payload(
        [Message(role="user", content="hello")],
        [],
        system_prompt="system",
    )

    assert "thinking" not in payload


def test_agent_propagates_connection_error_without_raising(tmp_path, monkeypatch) -> None:
    def fail_stream(*args, **kwargs):
        request = httpx.Request("POST", "https://api.deepseek.com/v1/chat/completions")
        raise httpx.ConnectError("temporary connection failure", request=request)

    monkeypatch.setattr(httpx.AsyncClient, "stream", fail_stream)
    config = PaiCliConfig()
    config.llm.api_key = "key"
    config.memory.long_term_db_path = str(tmp_path / "memory.db")
    agent = Agent(
        llm_client=_client(),
        tool_registry=ToolRegistry(),
        system_prompt="system",
        cwd=str(tmp_path),
        config=config,
    )

    events = asyncio.run(_collect_agent_events(agent))

    assert [event["type"] for event in events] == ["context_summary", "error"]
    error_event = next(event for event in events if event["type"] == "error")
    assert "Could not connect to deepseek" in str(error_event["error"])


def test_usage_only_chunk_without_choices_is_parsed() -> None:
    chunk = {
        "choices": [],
        "usage": {
            "prompt_tokens": 1_000,
            "completion_tokens": 200,
            "prompt_cache_hit_tokens": 600,
            "prompt_cache_miss_tokens": 400,
            "completion_tokens_details": {"reasoning_tokens": 50},
            "total_tokens": 1_200,
        },
    }

    events = asyncio.run(_collect_events(_client(), chunk))

    assert events == [
        {
            "type": "usage",
            "usage": {
                "input_tokens": 1_000,
                "output_tokens": 200,
                "cache_hit_tokens": 600,
                "cache_miss_tokens": 400,
                "reasoning_tokens": 50,
                "total_tokens": 1_200,
            },
        }
    ]


def test_usage_normalizes_old_and_provider_fields() -> None:
    legacy = Usage.from_mapping({"input_tokens": 10, "output_tokens": 5})
    provider = Usage.from_mapping(
        {
            "prompt_cache_hit_tokens": 7,
            "prompt_cache_miss_tokens": 3,
            "completion_tokens": 4,
            "reasoning_tokens": 2,
        }
    )

    assert legacy.total_tokens == 15
    assert legacy.to_dict()["input_tokens"] == 10
    assert provider.input_tokens == 10
    assert provider.output_tokens == 4
    assert provider.reasoning_tokens == 2
    assert provider.total_tokens == 14


def test_cache_usage_uses_deepseek_fields():
    usage = Usage.from_mapping(
        {
            "prompt_tokens": 1_000,
            "prompt_cache_hit_tokens": 600,
            "prompt_cache_miss_tokens": 400,
        }
    )

    assert usage.input_tokens == 1_000
    assert usage.cache_hit_tokens == 600
    assert usage.cache_miss_tokens == 400
    assert usage.cache_context_tokens == 1_000


def test_cache_usage_supports_harness_disjoint_buckets():
    usage = Usage.from_mapping(
        {
            "input_tokens": 1_000,
            "cache_read_input_tokens": 600,
            "cache_creation_input_tokens": 400,
        }
    )

    assert usage.input_tokens == 1_000
    assert usage.cache_hit_tokens == 600
    assert usage.cache_miss_tokens == 1_000
    assert usage.cache_write_tokens == 400
    assert usage.cache_context_tokens == 2_000
    assert (
        usage.cache_hit_tokens
        / (usage.cache_miss_tokens + usage.cache_hit_tokens + usage.cache_write_tokens)
        == 0.3
    )


def test_cache_usage_normalizes_harness_camel_case_buckets():
    usage = Usage.from_mapping(
        {
            "inputTokens": 1_000,
            "outputTokens": 10,
            "cacheReadTokens": 600,
            "cacheWriteTokens": 400,
        }
    )

    assert usage.input_tokens == 1_000
    assert usage.cache_hit_tokens == 600
    assert usage.cache_miss_tokens == 1_000
    assert usage.cache_write_tokens == 400
    assert usage.cache_context_tokens == 2_000
    assert usage.total_tokens == 1_010


def test_cache_usage_normalizes_harness_nested_details():
    usage = Usage.from_mapping(
        {
            "input_tokens": 1_000,
            "input_tokens_details": {"cache_read_tokens": 600, "cache_write_tokens": 400},
        }
    )

    assert usage.input_tokens == 1_000
    assert usage.cache_hit_tokens == 600
    assert usage.cache_write_tokens == 400
    assert usage.cache_miss_tokens == 1_000
    assert usage.cache_context_tokens == 2_000


def test_cache_usage_supports_compat_fields_and_only_derives_miss_when_hit_is_known():
    compat = Usage.from_mapping({"prompt_tokens": 1_000, "cached_tokens": 700})
    unknown = Usage.from_mapping({"prompt_tokens": 1_000})

    assert (compat.cache_hit_tokens, compat.cache_miss_tokens) == (700, 300)
    assert (unknown.cache_hit_tokens, unknown.cache_miss_tokens) == (0, 0)


def test_deepseek_v4_profiles_and_cost_formula() -> None:
    flash = get_builtin_price_profile("deepseek-flash")
    pro = get_builtin_price_profile("deepseek-v4-pro")
    assert flash is not None
    assert pro is not None
    assert flash.context_window == 1_000_000
    assert pro.context_window == 1_000_000
    assert flash.for_currency("usd").input_cache_hit == 0.0028
    assert flash.for_currency("cny").input_cache_miss == 1.0
    assert pro.for_currency("usd").output == 0.87
    assert pro.for_currency("cny").output == 6.0

    usage = Usage(
        input_tokens=1_000,
        output_tokens=200,
        cache_hit_tokens=600,
        cache_miss_tokens=400,
        reasoning_tokens=50,
    )
    usd = calculate_cost(usage, flash, currency="usd")
    cny = calculate_cost(usage, flash, currency="cny")

    assert usd.input_cache_hit_cost == pytest.approx(0.00000168)
    assert usd.input_cache_miss_cost == pytest.approx(0.000056)
    assert usd.output_cost == pytest.approx(0.000056)
    assert usd.total_cost == pytest.approx(0.00011368)
    assert cny.total_cost == pytest.approx(0.000812)
    assert usd.reasoning_tokens == 50


def test_uncategorized_legacy_input_uses_cache_miss_price() -> None:
    profile = get_builtin_price_profile("deepseek-flash")
    assert profile is not None

    cost = calculate_cost({"input_tokens": 1_000, "output_tokens": 0}, profile)

    assert cost.input_cache_hit_tokens == 0
    assert cost.input_cache_miss_tokens == 0
    assert cost.input_uncategorized_tokens == 1_000
    assert cost.input_uncategorized_cost == pytest.approx(0.00014)
    assert cost.total_cost == pytest.approx(0.00014)


def test_unknown_model_context_and_prices_can_be_configured(tmp_path) -> None:
    project_config = tmp_path / ".paicli" / "config.json"
    project_config.parent.mkdir(parents=True)
    project_config.write_text(
        json.dumps(
            {
                "llm": {
                    "provider": "openai-compatible",
                    "model": "private-coder",
                    "base_url": "https://llm.example/v1",
                    "context_window": 321_000,
                    "prices": {
                        "usd": {
                            "input_cache_hit": 0.1,
                            "input_cache_miss": 0.5,
                            "output": 1.5,
                        },
                        "cny": {
                            "input_cache_hit": 0.7,
                            "input_cache_miss": 3.5,
                            "output": 10.5,
                        },
                    },
                }
            }
        ),
        encoding="utf-8",
    )

    config = load_config(project_root=tmp_path, env={})
    client = create_llm_client(config.llm)

    assert client.max_context_window == 321_000
    assert client.price_profile is not None
    assert client.price_profile.as_of == "config"
    assert client.price_profile.for_currency("usd").output == 1.5
    assert client.calculate_cost(Usage(input_tokens=1_000), currency="usd").total_cost == (
        pytest.approx(0.0005)
    )


def test_deepseek_builtin_prices_allow_partial_config_override() -> None:
    client = create_llm_client(
        LlmConfig(
            provider="deepseek",
            model="deepseek-flash",
            context_window=900_000,
            prices={"usd": {"output": 9.0}},
        )
    )

    assert client.max_context_window == 900_000
    assert client.price_profile is not None
    assert client.price_profile.context_window == 900_000
    assert client.price_profile.for_currency("usd").input_cache_miss == 0.14
    assert client.price_profile.for_currency("usd").output == 9.0


async def _collect_events(
    client: OpenAICompatibleClient,
    chunk: dict,
) -> list[dict]:
    return [event async for event in client._parse_chunk(chunk)]


async def _collect_chat_events(client: OpenAICompatibleClient) -> list[dict]:
    return [
        event
        async for event in client.chat(
            [Message(role="user", content="hello")],
            [],
            system_prompt="system",
        )
    ]


async def _collect_agent_events(agent: Agent) -> list[dict]:
    return [event async for event in agent.run("hello")]


def _client() -> OpenAICompatibleClient:
    return OpenAICompatibleClient(
        provider_name="deepseek",
        model="deepseek-flash",
        api_key="key",
        base_url="https://api.deepseek.com/v1",
    )
