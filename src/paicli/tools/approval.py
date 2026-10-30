from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from paicli.tools.base import ToolDecision


@dataclass(slots=True)
class _Approval:
    request_id: str
    request: dict[str, Any]
    future: asyncio.Future[ToolDecision]
    status: str = "queued"


class ApprovalBroker:
    """Serializes terminal approval prompts shared by the parent and child agents."""

    def __init__(
        self,
        presenter: Callable[[dict[str, Any]], Awaitable[ToolDecision] | ToolDecision],
    ) -> None:
        self.presenter = presenter
        self._queue: asyncio.Queue[_Approval | None] = asyncio.Queue()
        self._worker: asyncio.Task[None] | None = None
        self._current: _Approval | None = None
        self._closed = False
        self.failure: Exception | None = None

    async def request(self, payload: dict[str, Any]) -> ToolDecision:
        if self.failure is not None:
            raise RuntimeError("Approval presenter failed; restart the approval broker.") from None
        if self._closed:
            return "deny"
        if self._worker is None:
            self._worker = asyncio.create_task(self._run())
        future: asyncio.Future[ToolDecision] = asyncio.get_running_loop().create_future()
        request = dict(payload)
        request_id = str(request.get("request_id") or uuid.uuid4().hex)
        request["request_id"] = request_id
        item = _Approval(request_id, request, future)
        await self._queue.put(item)
        try:
            return await future
        except asyncio.CancelledError:
            await self.cancel(request_id)
            raise

    def pending_requests(self) -> list[dict[str, Any]]:
        """Return safe attribution metadata for queued/active approvals."""
        result: list[dict[str, Any]] = []
        if self._current is not None:
            result.append({**self._current.request, "status": self._current.status})
        queued = list(getattr(self._queue, "_queue", ()))
        result.extend(
            {**item.request, "status": item.status} for item in queued if item is not None
        )
        return result

    async def cancel(self, request_id: str) -> bool:
        """Cancel a queued or active approval without allowing it to approve later."""
        if self._current and self._current.request_id == request_id:
            self._current.status = "canceled"
            if not self._current.future.done():
                self._current.future.set_result("deny")
            if self._worker is not None and not self._worker.done():
                self._worker.cancel()
                await asyncio.gather(self._worker, return_exceptions=True)
                self._current = None
                if not self._closed:
                    self._worker = asyncio.create_task(self._run())
            return True
        retained: list[_Approval | None] = []
        canceled = False
        while True:
            try:
                item = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if item is not None and item.request_id == request_id:
                item.status = "canceled"
                if not item.future.done():
                    item.future.set_result("deny")
                canceled = True
            else:
                retained.append(item)
        for item in retained:
            self._queue.put_nowait(item)
        return canceled

    async def _run(self) -> None:
        while True:
            item = await self._queue.get()
            if item is None:
                return
            if item.status == "canceled" or item.future.done():
                continue
            self._current = item
            item.status = "presenting"
            try:
                decision = self.presenter(item.request)
                if asyncio.iscoroutine(decision):
                    decision = await decision
            except Exception as exc:
                self.failure = exc
                item.status = "failed"
                if not item.future.done():
                    item.future.set_exception(RuntimeError("Approval presenter failed."))
                while not self._queue.empty():
                    pending = self._queue.get_nowait()
                    if pending is not None and not pending.future.done():
                        pending.status = "failed"
                        pending.future.set_exception(RuntimeError("Approval presenter failed."))
                self._current = None
                return
            if item.status != "canceled" and not item.future.done():
                item.status = "completed"
                item.future.set_result(decision)
            self._current = None

    async def close(self, *, timeout: float = 5.0) -> None:
        if self._closed and (self._worker is None or self._worker.done()):
            return
        self._closed = True
        while True:
            try:
                item = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if item is not None:
                item.status = "canceled"
                if not item.future.done():
                    item.future.set_result("deny")
        if self._worker is not None:
            if self._current is not None:
                self._current.status = "canceled"
                if not self._current.future.done():
                    self._current.future.set_result("deny")
                self._worker.cancel()
            else:
                self._queue.put_nowait(None)
            try:
                await asyncio.wait_for(self._worker, timeout=max(0.1, timeout))
            except asyncio.CancelledError:
                if self._closed:
                    return
                raise
            except TimeoutError as exc:
                self.failure = exc
                self._worker.cancel()
                await asyncio.gather(self._worker, return_exceptions=True)
                raise
