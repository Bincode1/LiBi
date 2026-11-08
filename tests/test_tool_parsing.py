from __future__ import annotations

import pytest

from libi.tools.call_parsing import (
    finalize_tool_calls,
    merge_tool_delta,
    tool_input,
    tool_name_by_id,
)


def test_interleaved_streaming_calls_reassemble_by_index():
    states = {}
    deltas = [
        {"index": 1, "function": {"name": "write", "arguments": '{"text":'}},
        {"index": 0, "function": {"arguments": '{"path":'}},
        {"index": 1, "id": "second", "function": {"arguments": '"hello"}'}},
        {"index": 0, "id": "first", "function": {"name": "read", "arguments": '"a"}'}},
        {"index": 2, "function": {"arguments": "{}"}},
    ]
    for delta in deltas:
        merge_tool_delta(states, delta)
    calls = finalize_tool_calls(states)
    assert [call["id"] for call in calls] == ["first", "second"]
    assert [tool_input(call) for call in calls] == [{"path": "a"}, {"text": "hello"}]
    assert tool_name_by_id(calls, "second") == "write"
    assert tool_name_by_id(calls, "missing") == "unknown"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", {}),
        ("{}", {}),
        ("[]", {"value": []}),
        ("null", {"value": None}),
        ("1", {"value": 1}),
        ("{broken", {"raw": "{broken"}),
    ],
)
def test_tool_argument_edge_cases(raw, expected):
    assert tool_input({"function": {"arguments": raw}}) == expected
