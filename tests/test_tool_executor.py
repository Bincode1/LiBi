from __future__ import annotations

import asyncio

import pytest

from libi.tools import ToolRegistry
from libi.tools.base import Tool, ToolResult, object_schema
from libi.tools.executor import ToolExecutor


def test_unknown_tool_and_missing_input_return_correlated_errors(tool_context):
    executed = []

    async def handler(payload, _context):
        executed.append(payload)
        return ToolResult("ok")

    registry = ToolRegistry()
    registry.register(
        Tool("inspect", "Inspect", object_schema({}), handler, required_keys=["path"])
    )
    results = asyncio.run(
        ToolExecutor(registry).execute_all(
            [
                {"id": "unknown", "name": "absent"},
                {"id": "invalid", "name": "inspect", "arguments": {}},
            ],
            tool_context,
        )
    )
    assert all(result.is_error for result in results)
    by_id = {result.tool_use_id: result for result in results}
    assert set(by_id) == {"invalid", "unknown"}
    assert "missing required input: path" in by_id["invalid"].content
    assert "not found" in by_id["unknown"].content
    assert executed == []


@pytest.mark.parametrize("failure", ["exception", "timeout", "returned_error"])
def test_tool_failure_is_explicit_and_finishes_status(tool_context, failure):
    statuses = []

    async def handler(_payload, _context):
        if failure == "exception":
            raise ValueError("handler failed")
        if failure == "timeout":
            await asyncio.Event().wait()
        return ToolResult("reported failure", is_error=True)

    async def status(event):
        statuses.append(event)

    registry = ToolRegistry()
    registry.register(Tool("inspect", "Inspect", object_schema({}), handler, timeout=0.02))
    tool_context.status_callback = status
    result = asyncio.run(
        ToolExecutor(registry).execute_all(
            [{"id": "failed", "name": "inspect", "arguments": {}}], tool_context
        )
    )[0]
    assert result.is_error and result.tool_use_id == "failed"
    assert [event["type"] for event in statuses] == ["tool_started", "tool_finished"]
    assert all(event["tool_call_id"] == "failed" for event in statuses)


@pytest.mark.parametrize(
    ("mode", "requires_approval", "decision", "approval_count", "allowed"),
    [
        pytest.param("auto", False, "deny", 0, True, id="auto-unprotected-bypasses"),
        pytest.param("never", True, "deny", 0, True, id="never-protected-bypasses"),
        pytest.param("auto", True, "approve", 1, True, id="auto-protected-approved"),
        pytest.param("auto", True, "deny", 1, False, id="auto-protected-denied"),
        pytest.param("auto", True, "skip", 1, False, id="auto-protected-skipped"),
        pytest.param("always", False, "approve", 1, True, id="always-unprotected-approved"),
        pytest.param("always", False, "deny", 1, False, id="always-unprotected-denied"),
        pytest.param("always", True, "skip", 1, False, id="always-protected-skipped"),
    ],
)
def test_tool_approval_policy(
    tool_context, mode, requires_approval, decision, approval_count, allowed
):
    approvals, executions = [], []

    async def handler(payload, _context):
        executions.append(payload)
        return ToolResult("ok")

    async def approve(request):
        approvals.append(request)
        return decision

    registry = ToolRegistry()
    registry.register(
        Tool("inspect", "Inspect", object_schema({}), handler, requires_approval=requires_approval)
    )
    tool_context.config.policy.hitl_mode = mode
    tool_context.approval_callback = approve
    result = asyncio.run(
        ToolExecutor(registry).execute_all(
            [{"id": "approval", "name": "inspect", "arguments": {}}], tool_context
        )
    )[0]
    assert len(approvals) == approval_count
    assert len(executions) == int(allowed)
    assert result.is_error is (not allowed)


def test_read_concurrency_is_bounded_and_writes_are_serial(tool_context):
    tool_context.config.policy.hitl_mode = "never"
    tool_context.config.tools.max_concurrent_read = 2
    active = peak = 0
    log = []

    async def run():
        ready = asyncio.Event()

        async def read(payload, _context):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            if active == 2:
                ready.set()
            await asyncio.wait_for(ready.wait(), timeout=1)
            log.append(("read", payload["index"]))
            active -= 1
            return ToolResult("read")

        async def write(payload, _context):
            assert active == 0
            log.append(("write", payload["index"]))
            return ToolResult("written")

        registry = ToolRegistry()
        registry.register(Tool("read", "Read", object_schema({}), read))
        registry.register(Tool("write", "Write", object_schema({}), write, is_read_only=False))
        calls = [
            {"id": str(i), "name": name, "arguments": {"index": i}}
            for i, name in enumerate(["write", "read", "read", "write", "read", "read"])
        ]
        results = await ToolExecutor(registry).execute_all(calls, tool_context)
        assert {result.tool_use_id for result in results} == {str(i) for i in range(6)}

    asyncio.run(run())
    assert peak == 2
    assert [name for name, _index in log] == ["read"] * 4 + ["write"] * 2
    assert [index for name, index in log if name == "write"] == [0, 3]


def test_tool_cancellation_propagates_and_notifies_completion(tool_context):
    async def run():
        started = asyncio.Event()
        finished = []

        async def handler(_payload, _context):
            started.set()
            await asyncio.Event().wait()

        async def status(event):
            finished.append(event["type"])

        tool_context.status_callback = status
        registry = ToolRegistry()
        registry.register(Tool("wait", "Wait", object_schema({}), handler))
        task = asyncio.create_task(
            ToolExecutor(registry).execute_all([{"id": "wait", "name": "wait"}], tool_context)
        )
        await asyncio.wait_for(started.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert finished == ["tool_started", "tool_finished"]

    asyncio.run(run())


def test_async_approval_presenter_failure_is_explicit(tool_context):
    async def handler(_payload, _context):
        pytest.fail("approval failure executed a protected tool")

    async def approval(_request):
        raise RuntimeError("approval presenter unavailable")

    registry = ToolRegistry()
    registry.register(
        Tool("protected", "Protected", object_schema({}), handler, requires_approval=True)
    )
    tool_context.approval_callback = approval
    result = asyncio.run(
        ToolExecutor(registry).execute_all([{"id": "protected", "name": "protected"}], tool_context)
    )[0]
    assert result.is_error and "approval presenter unavailable" in result.content
