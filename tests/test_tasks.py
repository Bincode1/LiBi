from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest

from libi.runtime import DurableTaskManager


@pytest.mark.parametrize("prompt", ["", " \n\t"])
def test_task_rejects_blank_prompt(tmp_path, prompt):
    manager = DurableTaskManager(tmp_path / "tasks.db")
    with pytest.raises(ValueError, match="cannot be empty"):
        manager.add(prompt)
    assert manager.list() == []


@pytest.mark.parametrize("finish", ["complete", "fail"])
def test_task_owner_and_terminal_transitions(tmp_path, finish):
    manager = DurableTaskManager(tmp_path / "tasks.db")
    task_id = manager.add("work")
    finish_task = getattr(manager, finish)
    assert not finish_task(task_id, "output", worker_id="worker-a")
    claimed = manager.claim_next("worker-a")
    assert claimed.id == task_id
    assert claimed.status == "running" and claimed.mode == "react"
    assert not finish_task(task_id, "wrong", worker_id="worker-b")
    assert not manager.heartbeat(task_id, "worker-b")
    assert manager.heartbeat(task_id, "worker-a")
    assert finish_task(task_id, "output", worker_id="worker-a")
    assert not finish_task(task_id, "late", worker_id="worker-a")
    assert not manager.cancel(task_id)
    assert not manager.heartbeat(task_id, "worker-a")
    task = manager.get(task_id)
    assert task.status == ("completed" if finish == "complete" else "failed")
    assert (task.result if finish == "complete" else task.error) == "output"
    assert task.worker_id is None and task.lease_expires_at is None


def test_task_lease_expiry_uses_controlled_time_and_rejects_stale_owner(tmp_path, monkeypatch):
    instant = datetime(2026, 1, 1, tzinfo=UTC)
    monkeypatch.setattr("libi.runtime.tasks._now", lambda: instant.isoformat())
    monkeypatch.setattr(
        "libi.runtime.tasks._future",
        lambda seconds: (instant + timedelta(seconds=seconds)).isoformat(),
    )
    manager = DurableTaskManager(tmp_path / "tasks.db")
    task_id = manager.add("work")
    manager.claim_next("old", lease_seconds=10)
    instant += timedelta(seconds=10)
    assert manager.requeue_expired() == 1
    assert manager.requeue_expired() == 0
    claimed = manager.claim_next("new")
    assert claimed.attempts == 2
    assert not manager.complete(task_id, "old result", worker_id="old")
    assert manager.complete(task_id, "new result", worker_id="new")


def test_task_scope_applies_to_reads_updates_and_cancellation(tmp_path):
    db = tmp_path / "tasks.db"
    a = DurableTaskManager(db, scope=tmp_path / "a")
    b = DurableTaskManager(db, scope=tmp_path / "b")
    task_id = a.add("a")
    a.claim_next("worker")
    assert b.get(task_id) is None
    assert b.list() == []
    assert not b.cancel(task_id)
    assert not b.heartbeat(task_id, "worker")
    assert not b.complete(task_id, "cross-scope", worker_id="worker")
    assert not b.fail(task_id, "cross-scope", worker_id="worker")
    assert b.requeue_expired() == 0
    assert a.complete(task_id, "owned", worker_id="worker")


def test_legacy_task_database_migrates_without_losing_tasks(tmp_path):
    path = tmp_path / "tasks.db"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "create table tasks (id text primary key, prompt text not null, status text not null, "
            "created_at text not null, updated_at text not null, result text, error text)"
        )
        conn.execute("insert into tasks values ('old', 'work', 'queued', '', '', null, null)")
    manager = DurableTaskManager(path)
    task = manager.claim_next()
    assert task.id == "old"
    assert task.mode == "react" and task.attempts == 1
    assert task.worker_id == "anonymous-worker"
    assert manager.complete(task.id, "done")
    assert manager.get(task.id).result == "done"


def test_concurrent_workers_claim_every_task_once_and_surface_thread_errors(tmp_path):
    manager = DurableTaskManager(tmp_path / "tasks.db")
    expected = {manager.add(f"task {index}") for index in range(12)}
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(manager.claim_next, f"worker-{i}") for i in range(16)]
        records = [future.result(timeout=5) for future in futures]
    claimed = [record.id for record in records if record is not None]
    assert len(claimed) == len(expected)
    assert set(claimed) == expected
    assert manager.claim_next() is None


def test_durable_task_cancel(tmp_path):
    manager = DurableTaskManager(tmp_path / "tasks.db")
    task_id = manager.add("do work")

    assert manager.cancel(task_id)
    assert manager.get(task_id).status == "canceled"  # type: ignore[union-attr]


def test_cancel_running_task_cannot_be_overwritten_by_worker_completion(tmp_path):
    manager = DurableTaskManager(tmp_path / "tasks.db")
    task_id = manager.add("do work")
    task = manager.claim_next("worker-1")
    assert task is not None

    assert manager.cancel(task_id)
    assert not manager.complete(task_id, "late result", worker_id="worker-1")

    canceled = manager.get(task_id)
    assert canceled is not None
    assert canceled.status == "canceling"
    assert canceled.result is None
    assert manager.finish_cancel(task)
    assert manager.get(task_id).status == "canceled"


def test_expired_worker_lease_is_reclaimed(tmp_path):
    manager = DurableTaskManager(tmp_path / "tasks.db")
    task_id = manager.add("do work", mode="plan")
    first = manager.claim_next("worker-1", lease_seconds=-1)
    assert first is not None
    assert not manager.heartbeat(task_id, "worker-1", attempt=first.attempts)
    assert not manager.complete(task_id, "expired", worker_id="worker-1", attempt=first.attempts)

    second = manager.claim_next("worker-1")
    assert second is not None
    assert not manager.heartbeat(task_id, "worker-1", attempt=first.attempts)
    assert manager.heartbeat(task_id, "worker-1", attempt=second.attempts)

    assert second.id == task_id
    assert second.worker_id == "worker-1"
    assert second.attempts == 2


def test_task_workers_do_not_consume_another_project_scope(tmp_path):
    db = tmp_path / "tasks.db"
    project_a = DurableTaskManager(db, scope=tmp_path / "a")
    project_b = DurableTaskManager(db, scope=tmp_path / "b")
    task_a = project_a.add("A")
    task_b = project_b.add("B")

    claimed_a = project_a.claim_next("worker-a")
    claimed_b = project_b.claim_next("worker-b")

    assert claimed_a is not None and claimed_a.id == task_a
    assert claimed_b is not None and claimed_b.id == task_b
    assert claimed_a.scope != claimed_b.scope
