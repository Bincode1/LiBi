from __future__ import annotations

import asyncio
import threading
import time

import pytest

from paicli.runtime.api import RuntimeApiServer, TaskCanceledError

pytestmark = pytest.mark.integration


@pytest.fixture
def runtime(tmp_path, config):
    config.llm.api_key = "synthetic-worker-credential"
    return RuntimeApiServer(cwd=str(tmp_path), config=config, api_key="synthetic", workers=0)


@pytest.mark.parametrize("mode", ["react", "plan"])
def test_worker_task_uses_real_engine_and_records_session(runtime, provider_stream, mode):
    import json

    batches, _requests = provider_stream
    if mode == "plan":
        batches.append(
            [
                {
                    "choices": [
                        {
                            "delta": {
                                "content": json.dumps(
                                    {
                                        "tasks": [
                                            {"id": "answer", "description": "Answer the request"}
                                        ]
                                    }
                                )
                            },
                            "finish_reason": "stop",
                        }
                    ]
                }
            ]
        )
    batches.append(
        [{"choices": [{"delta": {"content": "worker answer"}, "finish_reason": "stop"}]}]
    )
    task_id = runtime.task_manager.add("hello", mode=mode)
    claimed = runtime.task_manager.claim_next("worker")
    assert claimed.id == task_id
    text = asyncio.run(runtime._run_task(claimed))
    assert "worker answer" in text
    assert runtime.task_manager.complete(task_id, text, worker_id="worker")
    assert runtime.task_manager.get(task_id).status == "completed"
    assert runtime.session_store.list_sessions()[0].status == "completed"


def test_canceled_task_never_initializes_provider(runtime, monkeypatch):
    task_id = runtime.task_manager.add("work")
    claimed = runtime.task_manager.claim_next("worker")
    runtime.task_manager.cancel(task_id)

    async def unexpected(**_kwargs):
        pytest.fail("canceled task initialized tools")

    monkeypatch.setattr("paicli.runtime.api.build_tool_registry", unexpected)
    with pytest.raises(TaskCanceledError):
        asyncio.run(runtime._run_task(claimed))


@pytest.mark.parametrize("outcome", ["completed", "failed", "canceled"])
def test_worker_loop_settles_task_and_joins_heartbeat(runtime, monkeypatch, outcome):
    task_id = runtime.task_manager.add("work")

    async def execute(task):
        runtime._stop.set()
        if outcome == "failed":
            raise RuntimeError("worker failed")
        if outcome == "canceled":
            runtime.task_manager.cancel(task.id)
            raise TaskCanceledError(task.id)
        return "result"

    monkeypatch.setattr(runtime, "_run_task", execute)
    runtime._worker_loop("worker")
    task = runtime.task_manager.get(task_id)
    assert task.status == outcome
    if outcome == "failed":
        assert task.error == "worker failed"
    elif outcome == "completed":
        assert task.result == "result"


def test_running_worker_waits_for_tool_cleanup_before_canceled(runtime, monkeypatch, tmp_path):
    entered, canceled, release = (threading.Event() for _ in range(3))
    late_file = tmp_path / "late.txt"
    task_id = runtime.task_manager.add("simulate a suspended tool")

    async def execute(_task):
        entered.set()
        try:
            await asyncio.sleep(10)
            late_file.write_text("late write", encoding="utf-8")
        except asyncio.CancelledError:
            canceled.set()
            assert await asyncio.to_thread(release.wait, 5)
            raise

    monkeypatch.setattr(runtime, "_run_task", execute)
    worker = threading.Thread(target=runtime._worker_loop, args=("worker",), daemon=True)
    worker.start()
    try:
        assert entered.wait(5)
        assert runtime.task_manager.cancel(task_id)
        assert canceled.wait(5)
        assert runtime.task_manager.get(task_id).status == "canceling"
        release.set()
        deadline = time.monotonic() + 5
        while runtime.task_manager.get(task_id).status != "canceled":
            assert time.monotonic() < deadline
            time.sleep(0.02)
        assert not late_file.exists()
    finally:
        release.set()
        runtime._stop.set()
        worker.join(timeout=5)
        assert not worker.is_alive()


def test_runtime_cancellation_closes_turn_and_propagates(runtime, monkeypatch):
    sid = runtime._create_session()

    async def canceled(**_kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr("paicli.runtime.api.build_tool_registry", canceled)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(runtime._run_turn(sid, "hello"))
    state = runtime.session_store.state(sid)
    assert state.status == "canceled" and state.active_turn_id is None
    assert [event.event_type for event in runtime.session_store.events(sid)][-3:] == [
        "turn/started",
        "user/message",
        "turn/canceled",
    ]
