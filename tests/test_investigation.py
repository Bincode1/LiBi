from __future__ import annotations

import asyncio
import json
import runpy
from pathlib import Path

from paicli.agent import QueryEngine
from paicli.llm import create_llm_client
from paicli.tools.base import Tool, ToolResult, object_schema


def test_investigation_batches_reads_and_reports_evidence_before_verifying(
    tmp_path, config, tool_registry, provider_stream
):
    (tmp_path / "shipping.py").write_text(
        "def average_days(days):\n    return sum(days) / len(days)\n", encoding="utf-8"
    )
    (tmp_path / "README.md").write_text("No deliveries should return None.\n", encoding="utf-8")
    config.llm.api_key = "investigation-fixture"

    async def check_empty_input(_input, context):
        average_days = runpy.run_path(str(Path(context.cwd) / "shipping.py"))["average_days"]
        try:
            average_days([])
        except ZeroDivisionError:
            return ToolResult("Empty input raises ZeroDivisionError.", is_error=True)
        return ToolResult("Empty input did not raise.")

    tool_registry.register(
        Tool(
            "check_empty_input",
            "Verify the empty-input case.",
            object_schema({}),
            check_empty_input,
        )
    )
    batches, requests = provider_stream

    def call(index, name, **arguments):
        return {
            "index": index,
            "id": f"call-{index}-{name}",
            "function": {"name": name, "arguments": json.dumps(arguments)},
        }

    progress = "The return expression divides by the input length. I will verify the empty case."
    finding = "shipping.py:2 raises ZeroDivisionError for empty input instead of returning None."
    for delta, finish in (
        (
            {
                "tool_calls": [
                    call(0, "read_file", path="shipping.py"),
                    call(1, "read_file", path="README.md"),
                ]
            },
            "tool_calls",
        ),
        ({"content": progress, "tool_calls": [call(0, "check_empty_input")]}, "tool_calls"),
        ({"content": finding}, "stop"),
    ):
        batches.append([{"choices": [{"delta": delta, "finish_reason": finish}]}])

    engine = QueryEngine(
        llm_client=create_llm_client(config.llm),
        tool_registry=tool_registry,
        config=config,
        cwd=str(tmp_path),
    )

    async def run():
        async with engine:
            return await engine.ask_complete_async("Check the project for bugs.")

    result = asyncio.run(run())
    assert finding in result.text
    assert len(requests) == 3
    assert "Batch independent reads in the same response" in requests[0]["messages"][0]["content"]
    read_results = [
        message["content"] for message in requests[1]["messages"] if message["role"] == "tool"
    ]
    assert read_results == [
        "1: def average_days(days):\n2:     return sum(days) / len(days)",
        "1: No deliveries should return None.",
    ]
    history = engine.session_store.events(engine.session_id)
    note = next(event for event in history if event.payload.get("content") == progress)
    verification = next(
        event
        for event in history
        if event.payload.get("name") == "check_empty_input" and event.event_type == "tool/result"
    )
    assert note.event_type == "assistant/message"
    assert note.sequence < verification.sequence
    assert verification.payload["content"] == "Empty input raises ZeroDivisionError."
