from __future__ import annotations

import pytest

from paicli.context import CompactionPlanner, CompactionPolicy, estimate_text_tokens
from paicli.types import Message


def test_compaction_retains_complete_tool_batch():
    messages = [
        Message(role="user", content="old request"),
        Message(role="assistant", content="old answer"),
        Message(role="user", content="read file"),
        Message(role="assistant", content="", tool_calls=[{
            "id": "call-1", "type": "function", "function": {"name": "read_file", "arguments": "{}"}
        }]),
        Message(role="tool", content="result", tool_call_id="call-1"),
        Message(role="assistant", content="answer"),
    ]
    plan = CompactionPlanner(CompactionPolicy(
        10_000, 100, min_recent_messages=3
    )).plan(messages, force=True)
    assert plan.has_selection
    assert plan.retained_messages[0].role == "assistant"
    assert plan.retained_messages[0].tool_calls[0]["id"] == "call-1"
    assert plan.retained_messages[1].tool_call_id == "call-1"


def test_invalid_input_budget_is_explicit():
    with pytest.raises(ValueError, match="normal input budget"):
        _ = CompactionPolicy(100, 100).pressure_limit


def test_mixed_language_token_estimator_is_nonzero_and_conservative():
    assert estimate_text_tokens("你好 world()") >= 5
