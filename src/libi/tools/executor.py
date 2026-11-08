from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from libi.policy import AuditLog
from libi.tools.base import Tool, ToolContext, ToolDecision, ToolResult
from libi.tools.registry import ToolRegistry


class ToolExecutor:
    def __init__(
        self,
        registry: ToolRegistry,
        *,
        audit_log_path: str | None = None,
        sensitive_values: tuple[str, ...] = (),
    ):
        self.registry = registry
        # Construct the audit log once per executor (one per agent run) instead of
        # reopening the file handle for every single tool call.
        self._audit_log = AuditLog(audit_log_path) if audit_log_path else None
        if self._audit_log is not None:
            self._audit_log.redactor.add_sensitive_values(sensitive_values)

    async def execute_all(
        self,
        calls: list[dict[str, Any]],
        context: ToolContext,
        *,
        on_result: Callable[[ToolResult], None] | None = None,
    ) -> list[ToolResult]:
        read_calls: list[tuple[dict[str, Any], Tool]] = []
        sequential_calls: list[tuple[dict[str, Any], Tool | None]] = []

        for call in calls:
            name = _tool_call_name(call)
            tool = self.registry.get(name)
            if tool and tool.is_read_only and tool.is_concurrency_safe:
                read_calls.append((call, tool))
            else:
                sequential_calls.append((call, tool))

        async def execute(call: dict[str, Any], tool: Tool | None) -> ToolResult:
            result = await self._execute_single(call, tool, context)
            if on_result is not None:
                on_result(result)
            return result

        results: list[ToolResult] = []
        if read_calls:
            semaphore = asyncio.Semaphore(context.config.tools.max_concurrent_read)

            async def run_read(call: dict[str, Any], tool: Tool) -> ToolResult:
                async with semaphore:
                    return await execute(call, tool)

            results.extend(
                await asyncio.gather(*(run_read(call, tool) for call, tool in read_calls))
            )

        for call, tool in sequential_calls:
            results.append(await execute(call, tool))

        return results

    async def _execute_single(
        self,
        call: dict[str, Any],
        tool: Tool | None,
        context: ToolContext,
    ) -> ToolResult:
        tool_call_id = str(call.get("id") or "")
        name = _tool_call_name(call)
        payload = _tool_call_arguments(call)

        if not tool:
            return ToolResult(
                tool_use_id=tool_call_id,
                content=(
                    f'Tool "{name}" not found. Available tools: '
                    f"{', '.join(self.registry.list_names())}"
                ),
                is_error=True,
            )

        audit = self._audit_log
        if audit is None:
            audit = AuditLog(context.config.policy.audit_log_path)
        audit.redactor.add_sensitive_values((context.config.llm.api_key,))
        if context.session_store is not None:
            audit.redactor.add_sensitive_values(context.session_store.redactor.sensitive_values)
        approver = "none"

        def record_audit(data: dict[str, Any], outcome: str) -> None:
            try:
                audit.record(
                    tool_name=tool.name,
                    input_data=data,
                    outcome=outcome,
                    approver=approver,
                    cwd=context.cwd,
                )
            except OSError as exc:
                logging.getLogger(__name__).warning(
                    "Tool audit write failed: %s", type(exc).__name__
                )

        async def notify(kind: str, **fields: Any) -> None:
            if context.status_callback is not None:
                await context.status_callback(
                    {
                        "type": kind,
                        "tool_call_id": tool_call_id,
                        "tool_name": name,
                        **fields,
                    }
                )

        await notify("tool_started")
        summary = "Tool did not finish."
        try:
            data = tool.validate(payload)
            mode = context.config.policy.hitl_mode
            needs_approval = mode != "never" and (mode != "auto" or tool.requires_approval)
            decision: ToolDecision = "approve"
            if needs_approval:
                approver = "hitl"
                await notify("approval_waiting")
                try:
                    decision = await self._approval_decision(tool, data, context, tool_call_id)
                finally:
                    await notify("approval_resolved")
            if decision in {"deny", "skip"}:
                summary = f"Approval {decision}."
                record_audit(data, decision)
                return ToolResult(
                    tool_use_id=tool_call_id,
                    content=f'Tool "{tool.name}" was {decision}ed by approval policy.',
                    is_error=True,
                )
            if (
                not tool.is_read_only
                and tool.needs_workspace_checkpoint
                and context.before_workspace_mutation is not None
            ):
                context.before_workspace_mutation()
            result = await tool.execute(data, replace(context, tool_call_id=tool_call_id or None))
            summary = result.content
            result.tool_use_id = tool_call_id
            if not tool.is_read_only and context.config.features.audit_log:
                record_audit(data, "allow" if not result.is_error else "error")
            return result
        except Exception as exc:  # noqa: BLE001 - tool errors must flow back to the model
            summary = f"Tool failed ({type(exc).__name__})."
            if context.config.features.audit_log and not tool.is_read_only:
                record_audit(payload, "error")
            return ToolResult(
                tool_use_id=tool_call_id,
                content=f'Tool "{name}" execution error: {exc}',
                is_error=True,
            )
        finally:
            await notify("tool_finished", summary=summary)

    async def _approval_decision(
        self,
        tool: Tool,
        payload: dict[str, Any],
        context: ToolContext,
        tool_call_id: str,
    ) -> ToolDecision:
        if not context.approval_callback:
            return "deny"
        result = context.approval_callback(
            {
                "agent_id": context.agent_id or "root",
                "parent_id": context.parent_agent_id,
                "session_id": context.session_id,
                "cwd": context.cwd,
                "tool_name": tool.name,
                "tool_call_id": tool_call_id,
                "input": payload,
                "danger_level": tool.danger_level,
                "description": tool.description,
            }
        )
        if asyncio.iscoroutine(result):
            result = await result
        return result


def _tool_call_name(call: dict[str, Any]) -> str:
    function = call.get("function") if isinstance(call.get("function"), dict) else {}
    return str(function.get("name") or call.get("name") or "")


def _tool_call_arguments(call: dict[str, Any]) -> dict[str, Any]:
    function = call.get("function") if isinstance(call.get("function"), dict) else {}
    arguments = function.get("arguments", call.get("arguments", {}))
    if isinstance(arguments, str):
        import json

        try:
            parsed = json.loads(arguments or "{}")
        except json.JSONDecodeError:
            parsed = {"raw": arguments}
        return parsed if isinstance(parsed, dict) else {"value": parsed}
    return arguments if isinstance(arguments, dict) else {}
