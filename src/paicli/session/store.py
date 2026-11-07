from __future__ import annotations

import json
import logging
import os
import sqlite3
import uuid
from collections.abc import Callable
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from paicli.policy.redaction import Redactor
from paicli.session.models import (
    MAX_SESSION_TITLE_LENGTH,
    CompactionLease,
    SessionEvent,
    SessionState,
    SessionSummary,
)


def default_runtime_db_path() -> Path:
    """Return the canonical runtime SQLite database path under the user's home."""
    return Path.home() / ".paicli" / "runtime" / "runtime.db"


_MODEL_VISIBLE_EVENTS = {
    "user/message",
    "assistant/message",
    "tool/result",
    "memory/context",
    "skill/context",
    "compaction/checkpoint",
    "compaction/started",
    "compaction/failed",
}
_STATE_EVENT_TYPES = (
    "session/title_updated",
    "turn/started",
    "turn/completed",
    "turn/failed",
    "turn/canceled",
    "step/started",
    "step/completed",
    "step/failed",
    "compaction/checkpoint",
)


class TurnInProgressError(RuntimeError):
    pass


class SessionStore:
    """Transactional, append-only event storage backed by the Runtime SQLite database."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        workspace: str | Path | None = None,
        sensitive_values: tuple[str, ...] = (),
        on_append: Callable[[str], None] | None = None,
    ):
        self.db_path = Path(db_path).expanduser()
        self.workspace = (
            os.path.normcase(str(Path(workspace).resolve())) if workspace is not None else None
        )
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.redactor = Redactor(sensitive_values)
        self.runtime_port: int | None = None
        self._on_append = on_append
        self._ensure_schema()

    def create_session(
        self,
        session_id: str | None = None,
        *,
        kind: str = "root",
        parent_session_id: str | None = None,
    ) -> str:
        if kind not in {"root", "child"}:
            raise ValueError("session kind must be root or child")
        if kind == "child" and not parent_session_id:
            raise ValueError("child sessions require a parent session")
        if kind == "root" and parent_session_id is not None:
            raise ValueError("root sessions cannot have a parent session")
        identity = session_id or f"session_{uuid.uuid4().hex}"
        created_at = _now()
        with closing(self._connect()) as conn, conn:
            if parent_session_id is not None and self.workspace is not None:
                self._session_row(conn, parent_session_id)
            self._insert_session_tx(conn, identity, created_at, kind, parent_session_id)
            self._session_row(conn, identity)
        return identity

    def _insert_session_tx(
        self,
        conn: sqlite3.Connection,
        session_id: str,
        created_at: str,
        kind: str,
        parent_session_id: str | None,
    ) -> bool:
        cursor = conn.execute(
            "insert into sessions "
            "(id, created_at, updated_at, last_sequence, status, kind, parent_session_id, "
            "workspace) values (?, ?, ?, 0, 'created', ?, ?, ?) on conflict(id) do nothing",
            (session_id, created_at, created_at, kind, parent_session_id, self.workspace),
        )
        return cursor.rowcount == 1

    def start_child_session(
        self,
        parent_session_id: str,
        descriptor: dict[str, Any],
        *,
        turn_id: str | None,
        step_id: str | None,
        tool_call_id: str | None,
    ) -> str:
        """Create a child and publish its parent lifecycle entry in one transaction."""
        session_id = f"session_{uuid.uuid4().hex}"
        created_at = _now()
        started = {
            "child_id": descriptor["child_id"],
            "child_session_id": session_id,
            "description": descriptor["description"],
            "provider": descriptor["provider"],
        }
        if "role" in descriptor:
            started["role"] = descriptor["role"]
        descriptor_json = json.dumps(
            self.sanitize_payload({**descriptor, "parent_session_id": parent_session_id}),
            ensure_ascii=False,
            allow_nan=False,
        )
        started_json = json.dumps(
            self.sanitize_payload(started), ensure_ascii=False, allow_nan=False
        )
        with closing(self._connect()) as conn, conn:
            conn.execute("begin immediate")
            self._session_row(conn, parent_session_id)
            inserted = self._insert_session_tx(
                conn, session_id, created_at, "child", parent_session_id
            )
            if not inserted:
                raise sqlite3.IntegrityError("child session ID already exists")
            self._insert_event_tx(
                conn,
                session_id,
                "agent/subagent_descriptor",
                descriptor_json,
                created_at,
                turn_id=None,
                step_id=None,
                source="subagent-manager",
            )
            self._insert_event_tx(
                conn,
                parent_session_id,
                "agent/child_started",
                started_json,
                created_at,
                turn_id=turn_id,
                step_id=step_id,
                tool_call_id=tool_call_id,
                source="subagent-manager",
            )
        self._notify_append(session_id)
        self._notify_append(parent_session_id)
        return session_id

    def _notify_append(self, session_id: str) -> None:
        if self._on_append is not None:
            try:
                self._on_append(session_id)
            except Exception as exc:
                # The write has committed. A wake-up failure must not turn it into a failed write.
                logging.getLogger(__name__).warning(
                    "Session event notification failed: %s", type(exc).__name__
                )

    def _session_row(self, conn: sqlite3.Connection, session_id: str) -> tuple[str, int]:
        row = conn.execute(
            "select status, last_sequence, workspace from sessions where id = ?", (session_id,)
        ).fetchone()
        if row is None or (self.workspace is not None and row[2] != self.workspace):
            raise ValueError(f"session not found in this workspace: {session_id}")
        return str(row[0]), int(row[1] or 0)

    def adopt_session(self, session_id: str) -> None:
        """Assign a settled legacy session tree to an explicitly selected workspace."""
        if self.workspace is None:
            raise ValueError("a workspace is required to adopt a session")
        with closing(self._connect()) as conn, conn:
            conn.execute("begin immediate")
            root = conn.execute(
                "select workspace, status from sessions where id = ? and kind = 'root'",
                (session_id,),
            ).fetchone()
            if root is None or root[0] not in {None, self.workspace}:
                raise ValueError(f"session cannot be adopted in this workspace: {session_id}")
            if root[0] == self.workspace:
                return
            if root[1] == "running":
                raise TurnInProgressError("an active legacy session cannot be adopted")
            conn.execute(
                "with recursive tree(id) as (select ? union "
                "select s.id from sessions s join tree on s.parent_session_id = tree.id "
                "where s.workspace is null) update sessions set workspace = ? "
                "where id in (select id from tree)",
                (session_id, self.workspace),
            )

    def start_turn(self, session_id: str, turn_id: str, *, mode: str, message: str) -> int:
        """Claim a session and persist its input before an executor can run it."""
        started: dict[str, Any] = {"mode": mode}
        if self.runtime_port is not None:
            started["runtime_port"] = self.runtime_port
        created_at = _now()
        with closing(self._connect()) as conn, conn:
            conn.execute("begin immediate")
            status, after_sequence = self._session_row(conn, session_id)
            if status == "running":
                raise TurnInProgressError("session already has an active turn")
            self._assert_write_allowed(conn, session_id, "user/message", None)
            for event_type, payload in (
                ("turn/started", started),
                ("user/message", {"content": message}),
            ):
                self._insert_event_tx(
                    conn,
                    session_id,
                    event_type,
                    json.dumps(self.sanitize_payload(payload), ensure_ascii=False),
                    created_at,
                    turn_id=turn_id,
                    step_id=None,
                    source="cli-or-runtime",
                )
        self._notify_append(session_id)
        return after_sequence

    def session_exists(self, session_id: str) -> bool:
        """Return whether a persisted session can be resumed by its identifier."""
        if not session_id.strip():
            return False
        with closing(self._connect()) as conn:
            if self.workspace is not None:
                return (
                    conn.execute(
                        "select 1 from sessions where id = ? and workspace = ?",
                        (session_id, self.workspace),
                    ).fetchone()
                    is not None
                )
            row = conn.execute(
                "select 1 from sessions where id = ? "
                "union select 1 from events where session_id = ? limit 1",
                (session_id, session_id),
            ).fetchone()
        return row is not None

    def claim_session_title(self, session_id: str, turn_id: str) -> bool:
        """Reserve one automatic title attempt for the first real input."""
        with closing(self._connect()) as conn, conn:
            conn.execute("begin immediate")
            self._session_row(conn, session_id)
            cursor = conn.execute(
                """
                update sessions set title_generation_claimed = 1
                where id = ? and kind = 'root' and title is null and title_generation_claimed = 0
                  and ? = (
                    select turn_id from events
                    where session_id = ? and type = 'user/message'
                      and coalesce(source, '') not in ('subagent-delivery', 'subagent-steer')
                      and coalesce(source, '') not like 'child:%'
                    order by sequence limit 1
                  )
                """,
                (session_id, turn_id, session_id),
            )
            return cursor.rowcount == 1

    def set_session_title(
        self, session_id: str, title: str, *, generated: bool = False
    ) -> str | None:
        if not isinstance(title, str):
            raise ValueError("title must be a string")
        title = " ".join(title.split())
        if not 1 <= len(title) <= MAX_SESSION_TITLE_LENGTH:
            raise ValueError(f"title must be 1 to {MAX_SESSION_TITLE_LENGTH} characters")
        title = self.sanitize_payload({"title": title})["title"][:MAX_SESSION_TITLE_LENGTH].rstrip()
        with closing(self._connect()) as conn, conn:
            conn.execute("begin immediate")
            self._session_row(conn, session_id)
            current = conn.execute(
                "select title from sessions where id = ? and kind = 'root'", (session_id,)
            ).fetchone()
            if current is None:
                raise ValueError("title requires a root session")
            if generated and current[0] is not None:
                return None
            if current[0] == title:
                return title
            conn.execute("update sessions set title = ? where id = ?", (title, session_id))
            self._insert_event_tx(
                conn,
                session_id,
                "session/title_updated",
                json.dumps({"title": title}, ensure_ascii=False),
                _now(),
                turn_id=None,
                step_id=None,
                source="title-generator" if generated else "runtime",
            )
        self._notify_append(session_id)
        return title

    def last_sequence(self, session_id: str) -> int:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "select last_sequence from sessions where id = ?", (session_id,)
            ).fetchone()
        return int(row[0] or 0) if row else 0

    def list_sessions(
        self,
        *,
        limit: int | None = 100,
        include_children: bool = False,
        parent_session_id: str | None = None,
    ) -> list[SessionSummary]:
        """Return indexed session metadata without loading event payloads."""
        if limit is not None and limit < 1:
            raise ValueError("session list limit must be at least 1")
        with closing(self._connect()) as conn:
            filters = []
            params: list[Any] = []
            if parent_session_id is not None:
                filters.extend(["kind = 'child'", "parent_session_id = ?"])
                params.append(parent_session_id)
            elif not include_children:
                filters.append("kind = 'root'")
            if self.workspace is not None:
                filters.append("workspace = ?")
                params.append(self.workspace)
            where = "where " + " and ".join(filters) if filters else ""
            rows = conn.execute(
                "select id, created_at, updated_at, last_sequence, status, kind, "
                "parent_session_id, title "
                f"from sessions {where} order by updated_at desc, id desc limit ?",
                (*params, limit if limit is not None else -1),
            ).fetchall()

        return [
            SessionSummary(
                session_id=str(session_id),
                created_at=str(created_at),
                updated_at=str(updated_at),
                status=str(status),
                last_sequence=int(last_sequence or 0),
                kind=str(kind or "root"),
                parent_session_id=str(parent_session_id) if parent_session_id else None,
                title=self.redactor.clean(title) if title is not None else None,
            )
            for (
                session_id,
                created_at,
                updated_at,
                last_sequence,
                status,
                kind,
                parent_session_id,
                title,
            ) in rows
        ]

    def is_child_session(self, parent_session_id: str, child_session_id: str) -> bool:
        """Check the stored ancestry within the current workspace."""
        if parent_session_id == child_session_id:
            return False
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                with recursive ancestors(id, parent_session_id, kind) as (
                    select id, parent_session_id, kind from sessions
                    where id = ? and kind = 'child' and (? is null or workspace = ?)
                    union
                    select s.id, s.parent_session_id, s.kind from sessions s join ancestors a
                      on s.id = a.parent_session_id and a.kind = 'child'
                    where (? is null or s.workspace = ?)
                )
                select 1 from ancestors where id = ?
                """,
                (
                    child_session_id,
                    self.workspace,
                    self.workspace,
                    self.workspace,
                    self.workspace,
                    parent_session_id,
                ),
            ).fetchone()
        return row is not None

    def child_parent(self, child_session_id: str) -> str | None:
        with closing(self._connect()) as conn:
            self._session_row(conn, child_session_id)
            row = conn.execute(
                "select parent_session_id from sessions where id = ? and kind = 'child'",
                (child_session_id,),
            ).fetchone()
        return row[0] if row else None

    def append(
        self,
        session_id: str,
        event_type: str,
        payload: dict[str, Any],
        *,
        turn_id: str | None = None,
        step_id: str | None = None,
        tool_call_id: str | None = None,
        approval_id: str | None = None,
        source: str = "agent",
        schema_version: int = 1,
        lease_token: str | None = None,
    ) -> SessionEvent:
        if not event_type.strip():
            raise ValueError("session event type cannot be empty")
        if schema_version != 1:
            raise ValueError(f"unsupported session event schema version: {schema_version}")
        if event_type == "turn/started" and self.runtime_port is not None:
            payload = {**payload, "runtime_port": self.runtime_port}
        cleaned = self.sanitize_payload(payload)
        payload_json = json.dumps(
            cleaned, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        created_at = _now()
        conn = self._connect()
        try:
            conn.execute("begin immediate")
            if self.workspace is not None:
                self._session_row(conn, session_id)
            else:
                conn.execute(
                    "insert or ignore into sessions"
                    "(id, created_at, updated_at, last_sequence, status, kind) "
                    "values (?, ?, ?, 0, ?, ?)",
                    (session_id, created_at, created_at, "created", "root"),
                )
            self._assert_write_allowed(conn, session_id, event_type, lease_token)
            event_id, sequence = self._insert_event_tx(
                conn,
                session_id,
                event_type,
                payload_json,
                created_at,
                turn_id=turn_id,
                step_id=step_id,
                tool_call_id=tool_call_id,
                approval_id=approval_id,
                source=source,
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        self._notify_append(session_id)
        return SessionEvent(
            event_id=event_id,
            sequence=sequence,
            session_id=session_id,
            turn_id=turn_id,
            step_id=step_id,
            tool_call_id=tool_call_id,
            approval_id=approval_id,
            event_type=event_type,
            source=source,
            payload=cleaned,
            created_at=created_at,
            schema_version=schema_version,
        )

    def acquire_compaction_lease(
        self,
        session_id: str,
        *,
        attempt_id: str,
        base_sequence: int,
        surface_hash: str,
        ttl_seconds: float = 300.0,
    ) -> CompactionLease:
        """Acquire the per-session fencing lease used by compaction coordinators."""
        if not attempt_id.strip() or not surface_hash.strip():
            raise ValueError("compaction lease requires attempt_id and surface_hash")
        if base_sequence < 0 or ttl_seconds <= 0:
            raise ValueError("invalid compaction lease parameters")
        now = datetime.now(UTC)
        expires = now.timestamp() + float(ttl_seconds)
        expires_at = datetime.fromtimestamp(expires, UTC).isoformat()
        owner_token = f"lease_{uuid.uuid4().hex}"
        conn = self._connect()
        try:
            conn.execute("begin immediate")
            if self.workspace is not None:
                self._session_row(conn, session_id)
            else:
                conn.execute(
                    "insert or ignore into sessions"
                    "(id, created_at, updated_at, last_sequence, status, kind) "
                    "values (?, ?, ?, 0, ?, ?)",
                    (session_id, now.isoformat(), now.isoformat(), "created", "root"),
                )
            current_sequence = int(
                conn.execute(
                    "select coalesce(max(sequence), 0) from events where session_id = ?",
                    (session_id,),
                ).fetchone()[0]
            )
            if current_sequence < base_sequence or (
                current_sequence > base_sequence
                and conn.execute(
                    "select 1 from events where session_id = ? and sequence > ? "
                    "and type != 'session/title_updated' limit 1",
                    (session_id, base_sequence),
                ).fetchone()
            ):
                raise RuntimeError("session history changed before compaction lease acquisition")
            row = conn.execute(
                "select owner_token, expires_at from session_leases where session_id = ?",
                (session_id,),
            ).fetchone()
            if row is not None:
                try:
                    active = datetime.fromisoformat(str(row[1])) > now
                except ValueError:
                    active = False
                if active:
                    raise RuntimeError("session compaction is busy")
                conn.execute("delete from session_leases where session_id = ?", (session_id,))
            conn.execute(
                "insert into session_leases(session_id, owner_token, attempt_id, base_sequence, "
                "surface_hash, expires_at, runtime_port) values (?, ?, ?, ?, ?, ?, ?)",
                (
                    session_id,
                    owner_token,
                    attempt_id,
                    base_sequence,
                    surface_hash,
                    expires_at,
                    self.runtime_port,
                ),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return CompactionLease(
            session_id, owner_token, attempt_id, base_sequence, surface_hash, expires_at
        )

    def renew_compaction_lease(
        self, lease_token: str, *, ttl_seconds: float = 300.0
    ) -> CompactionLease:
        if not lease_token or ttl_seconds <= 0:
            raise ValueError("invalid compaction lease renewal")
        now = datetime.now(UTC)
        expires_at = datetime.fromtimestamp(now.timestamp() + ttl_seconds, UTC).isoformat()
        conn = self._connect()
        try:
            conn.execute("begin immediate")
            row = conn.execute(
                "select session_id, attempt_id, base_sequence, surface_hash, expires_at "
                "from session_leases where owner_token = ?",
                (lease_token,),
            ).fetchone()
            if row is None or _is_expired(row[4], now):
                raise RuntimeError("compaction lease is expired or invalid")
            conn.execute(
                "update session_leases set expires_at = ? where owner_token = ?",
                (expires_at, lease_token),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return CompactionLease(
            str(row[0]), lease_token, str(row[1]), int(row[2]), str(row[3]), expires_at
        )

    def commit_compaction_checkpoint(
        self,
        lease_token: str,
        payload: dict[str, Any],
        *,
        turn_id: str | None = None,
        step_id: str | None = None,
        source: str = "compaction-coordinator",
    ) -> SessionEvent:
        return self._finish_compaction(
            lease_token,
            "compaction/checkpoint",
            payload,
            turn_id=turn_id,
            step_id=step_id,
            source=source,
        )

    def fail_compaction(
        self,
        lease_token: str,
        payload: dict[str, Any],
        *,
        turn_id: str | None = None,
        step_id: str | None = None,
        source: str = "compaction-coordinator",
    ) -> SessionEvent:
        return self._finish_compaction(
            lease_token,
            "compaction/failed",
            payload,
            turn_id=turn_id,
            step_id=step_id,
            source=source,
        )

    def _finish_compaction(
        self,
        lease_token: str,
        event_type: str,
        payload: dict[str, Any],
        *,
        turn_id: str | None,
        step_id: str | None,
        source: str,
    ) -> SessionEvent:
        if event_type not in {"compaction/checkpoint", "compaction/failed"}:
            raise ValueError("invalid compaction completion event")
        cleaned = self.sanitize_payload(payload)
        payload_json = json.dumps(
            cleaned, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        now = _now()
        conn = self._connect()
        try:
            conn.execute("begin immediate")
            row = conn.execute(
                "select session_id, expires_at from session_leases where owner_token = ?",
                (lease_token,),
            ).fetchone()
            if row is None or _is_expired(row[1], datetime.now(UTC)):
                raise RuntimeError("compaction lease is expired or invalid")
            session_id = str(row[0])
            event_id, sequence = self._insert_event_tx(
                conn,
                session_id,
                event_type,
                payload_json,
                now,
                turn_id=turn_id,
                step_id=step_id,
                source=source,
            )
            conn.execute("delete from session_leases where owner_token = ?", (lease_token,))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        self._notify_append(session_id)
        return SessionEvent(
            event_id,
            sequence,
            session_id,
            turn_id,
            step_id,
            None,
            None,
            event_type,
            source,
            cleaned,
            now,
            1,
        )

    def active_turn_step(self, session_id: str) -> tuple[str | None, str | None]:
        with closing(self._connect()) as conn:
            row = conn.execute(
                """
                select start.turn_id, (
                    select case when step.type = 'step/started' then step.step_id end
                    from events step where step.session_id = start.session_id
                      and step.turn_id = start.turn_id
                      and step.type in ('step/started', 'step/completed', 'step/failed')
                    order by step.sequence desc limit 1
                )
                from sessions s join events start on start.session_id = s.id
                where s.id = ? and s.status = 'running' and start.type = 'turn/started'
                order by start.sequence desc limit 1
                """,
                (session_id,),
            ).fetchone()
        return (row[0], row[1]) if row else (None, None)

    def pending_tool_calls(self, session_id: str, turn_id: str, step_id: str) -> dict[str, str]:
        with closing(self._connect()) as conn:
            return self._pending_tool_calls(conn, session_id, turn_id, step_id)

    @staticmethod
    def _pending_tool_calls(
        conn: sqlite3.Connection, session_id: str, turn_id: str, step_id: str
    ) -> dict[str, str]:
        rows = conn.execute(
            """
            select type, case when type = 'assistant/message' then payload end, tool_call_id
            from events where session_id = ? and turn_id = ? and step_id = ?
              and type in ('assistant/message', 'tool/result') and schema_version is not null
              and sequence > coalesce((
                  select max(sequence) from events where session_id = ? and turn_id = ?
                    and step_id = ? and type = 'step/started'
              ), 0)
            order by sequence
            """,
            (session_id, turn_id, step_id, session_id, turn_id, step_id),
        )
        pending: dict[str, str] = {}
        for event_type, payload, call_id in rows:
            if event_type == "assistant/message":
                for call in json.loads(payload).get("tool_calls") or []:
                    pending[call["id"]] = call["function"]["name"]
            elif call_id:
                pending.pop(call_id, None)
        return pending

    def recover_interrupted_runtime_turns(self, port: int) -> None:
        """Settle turns from the preceding owner after exclusively binding its listener."""
        now = datetime.now(UTC)
        created_at = now.isoformat()
        with closing(self._connect()) as conn, conn:
            conn.execute("begin immediate")
            conn.execute(
                "delete from session_leases where runtime_port = ? and (? is null or "
                "session_id in (select id from sessions where workspace = ?))",
                (port, self.workspace, self.workspace),
            )
            turns = conn.execute(
                """
                select s.id, start.turn_id, start.payload, lease.expires_at
                from sessions s
                join events start on start.session_id = s.id and start.sequence = (
                    select max(sequence) from events
                    where session_id = s.id and type = 'turn/started'
                )
                left join session_leases lease on lease.session_id = s.id
                where s.status = 'running' and start.turn_id is not null
                  and (? is null or s.workspace = ?)
                """,
                (self.workspace, self.workspace),
            ).fetchall()
            for session_id, turn_id, payload, lease_expires_at in turns:
                if json.loads(payload).get("runtime_port") != port:
                    continue
                if lease_expires_at is not None and not _is_expired(lease_expires_at, now):
                    continue
                step = conn.execute(
                    """
                    select type, step_id from events where session_id = ? and turn_id = ?
                      and type in ('step/started', 'step/completed', 'step/failed')
                    order by sequence desc limit 1
                    """,
                    (session_id, turn_id),
                ).fetchone()
                if step and step[0] == "step/started" and step[1]:
                    for call_id, name in self._pending_tool_calls(
                        conn, session_id, turn_id, step[1]
                    ).items():
                        self._insert_event_tx(
                            conn,
                            session_id,
                            "tool/result",
                            json.dumps(
                                {
                                    "name": name,
                                    "content": "Tool call interrupted by Runtime restart. "
                                    "Result unknown.",
                                    "is_error": True,
                                }
                            ),
                            created_at,
                            turn_id=turn_id,
                            step_id=step[1],
                            tool_call_id=call_id,
                            source="runtime",
                        )
                approvals = conn.execute(
                    """
                    select approval_id, tool_call_id, step_id from events
                    where session_id = ? and turn_id = ? and type = 'approval/started'
                      and approval_id is not null and approval_id not in (
                        select approval_id from events where session_id = ? and turn_id = ?
                          and type in (
                              'approval/approved', 'approval/rejected', 'approval/canceled'
                          )
                          and approval_id is not null
                    )
                    order by sequence
                    """,
                    (session_id, turn_id, session_id, turn_id),
                ).fetchall()
                detail = json.dumps({"reason": "runtime_restarted"})
                for approval_id, call_id, step_id in approvals:
                    self._insert_event_tx(
                        conn,
                        session_id,
                        "approval/canceled",
                        detail,
                        created_at,
                        turn_id=turn_id,
                        step_id=step_id,
                        tool_call_id=call_id,
                        approval_id=approval_id,
                        source="runtime",
                    )
                self._insert_event_tx(
                    conn,
                    session_id,
                    "turn/canceled",
                    detail,
                    created_at,
                    turn_id=turn_id,
                    step_id=None,
                    source="runtime",
                )

    def events(
        self,
        session_id: str,
        *,
        after_sequence: int = 0,
        event_types: tuple[str, ...] | None = None,
    ) -> list[SessionEvent]:
        if after_sequence < 0:
            raise ValueError("event cursor cannot be negative")
        if event_types == ():
            return []
        type_filter = ""
        if event_types is not None:
            placeholders = ", ".join("?" for _ in event_types)
            type_filter = f"and type in ({placeholders})"
        with closing(self._connect()) as conn, conn:
            if self.workspace is not None:
                self._session_row(conn, session_id)
            rows = conn.execute(
                f"""
                select event_id, sequence, session_id, turn_id, step_id, tool_call_id,
                       approval_id, type, source, payload, created_at, schema_version
                from events
                where session_id = ? and sequence > ? and schema_version is not null
                {type_filter}
                order by sequence
                """,
                (session_id, after_sequence, *(event_types or ())),
            ).fetchall()
        return self._decode_events(rows)

    def replay_events(
        self,
        session_id: str,
        *,
        after_sequence: int,
        visible_after_sequence: int,
        visible_types: tuple[str, ...],
        state_event_types: tuple[str, ...] = (),
    ) -> tuple[list[SessionEvent], int]:
        """Read events needed for state and the undelivered conversation through a watermark."""
        state_events = _STATE_EVENT_TYPES + state_event_types
        state_types = ",".join("?" for _ in state_events)
        replay_types = ",".join("?" for _ in visible_types) or "null"
        with closing(self._connect()) as conn, conn:
            if self.workspace is not None:
                self._session_row(conn, session_id)
            latest = conn.execute(
                "select sequence from events where session_id = ? and schema_version is not null "
                "order by sequence desc limit 1",
                (session_id,),
            ).fetchone()
            if latest is None or latest[0] <= after_sequence:
                return [], after_sequence
            watermark = latest[0]
            # Bound this read to the watermark so a concurrent append is delivered next time.
            rows = conn.execute(
                f"""
                select event_id, sequence, session_id, turn_id, step_id, tool_call_id,
                       approval_id, type, source, payload, created_at, schema_version
                from events
                where session_id = ? and sequence > ? and sequence <= ?
                    and schema_version is not null
                    and (type in ({state_types}) or
                         (sequence > ? and type in ({replay_types})))
                order by sequence
                """,
                (
                    session_id,
                    after_sequence,
                    watermark,
                    *state_events,
                    visible_after_sequence,
                    *visible_types,
                ),
            ).fetchall()
        return self._decode_events(rows), watermark

    def _decode_events(self, rows: list[tuple[Any, ...]]) -> list[SessionEvent]:
        result = []
        for row in rows:
            try:
                payload = json.loads(row[9])
            except json.JSONDecodeError as exc:
                raise ValueError(f"corrupt session event payload at sequence {row[1]}") from exc
            if not isinstance(payload, dict):
                raise ValueError(f"session event payload at sequence {row[1]} is not an object")
            result.append(
                SessionEvent(
                    event_id=row[0],
                    sequence=row[1],
                    session_id=row[2],
                    turn_id=row[3],
                    step_id=row[4],
                    tool_call_id=row[5],
                    approval_id=row[6],
                    event_type=row[7],
                    source=row[8],
                    payload=self.sanitize_payload(payload),
                    created_at=row[10],
                    schema_version=row[11],
                )
            )
        return result

    def state(self, session_id: str) -> SessionState:
        events, watermark = self.replay_events(
            session_id, after_sequence=0, visible_after_sequence=0, visible_types=()
        )
        state = self.advance_state(SessionState(session_id, "created", 0), events)
        return replace(state, last_sequence=watermark)

    @staticmethod
    def advance_state(state: SessionState, events: list[SessionEvent]) -> SessionState:
        turn_id = state.active_turn_id
        step_id = state.active_step_id
        status = state.status
        compaction_id = state.latest_compaction_id
        title = state.title
        for event in events:
            if event.event_type == "session/title_updated":
                title = event.payload["title"]
            elif event.event_type == "turn/started":
                turn_id, status = event.turn_id, "running"
            elif event.event_type in {"turn/completed", "turn/failed", "turn/canceled"}:
                turn_id, step_id, status = None, None, event.event_type.split("/", 1)[1]
            elif event.event_type == "step/started":
                step_id = event.step_id
            elif event.event_type in {"step/completed", "step/failed"}:
                step_id = None
            elif event.event_type == "compaction/checkpoint":
                compaction_id = str(event.payload.get("compaction_id") or event.event_id)
        return SessionState(
            session_id=state.session_id,
            status=status,
            last_sequence=events[-1].sequence if events else state.last_sequence,
            active_turn_id=turn_id,
            active_step_id=step_id,
            latest_compaction_id=compaction_id,
            title=title,
        )

    def sanitize_payload(self, payload: dict[str, Any]) -> dict[str, Any]:
        cleaned = self.redactor.clean(payload)
        if not isinstance(cleaned, dict):
            raise TypeError("session event payload must be a JSON object")
        return cleaned

    @staticmethod
    def _assert_write_allowed(
        conn: sqlite3.Connection,
        session_id: str,
        event_type: str,
        lease_token: str | None,
    ) -> None:
        if event_type not in _MODEL_VISIBLE_EVENTS and not event_type.startswith("compaction/"):
            return
        row = conn.execute(
            "select owner_token, expires_at from session_leases where session_id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            return
        if _is_expired(row[1], datetime.now(UTC)):
            conn.execute("delete from session_leases where session_id = ?", (session_id,))
            return
        if lease_token != row[0] or _is_expired(row[1], datetime.now(UTC)):
            raise RuntimeError("session compaction is busy")

    @staticmethod
    def _insert_event_tx(
        conn: sqlite3.Connection,
        session_id: str,
        event_type: str,
        payload_json: str,
        created_at: str,
        *,
        turn_id: str | None,
        step_id: str | None,
        source: str,
        tool_call_id: str | None = None,
        approval_id: str | None = None,
    ) -> tuple[str, int]:
        event_id = f"evt_{uuid.uuid4().hex}"
        sequence = int(
            conn.execute(
                "select coalesce(max(sequence), 0) + 1 from events where session_id = ?",
                (session_id,),
            ).fetchone()[0]
        )
        conn.execute(
            "insert into events(session_id, type, payload, created_at, event_id, sequence, "
            "turn_id, "
            "step_id, tool_call_id, approval_id, source, schema_version) "
            "values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                session_id,
                event_type,
                payload_json,
                created_at,
                event_id,
                sequence,
                turn_id,
                step_id,
                tool_call_id,
                approval_id,
                source,
                1,
            ),
        )
        conn.execute(
            "update sessions set updated_at = case ? when 'session/title_updated' "
            "then updated_at else ? end, last_sequence = ?, "
            "status = case ? "
            "when 'turn/started' then 'running' "
            "when 'turn/completed' then 'completed' "
            "when 'turn/failed' then 'failed' "
            "when 'turn/canceled' then 'canceled' "
            "else coalesce(status, 'created') end where id = ?",
            (event_type, created_at, sequence, event_type, session_id),
        )
        return event_id, sequence

    def _ensure_schema(self) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute("begin immediate")
            conn.execute(
                """
                create table if not exists sessions (
                    id text primary key,
                    created_at text not null,
                    updated_at text,
                    last_sequence integer,
                    status text
                )
                """
            )
            session_columns = {row[1] for row in conn.execute("pragma table_info(sessions)")}
            session_additions = {
                "updated_at": "text",
                "last_sequence": "integer",
                "status": "text",
                "kind": "text",
                "parent_session_id": "text",
                "workspace": "text",
                "title": "text",
                "title_generation_claimed": "integer not null default 0",
            }
            for column, column_type in session_additions.items():
                if column not in session_columns:
                    conn.execute(f"alter table sessions add column {column} {column_type}")
            conn.execute(
                """
                create table if not exists events (
                    id integer primary key autoincrement,
                    session_id text not null,
                    type text not null,
                    payload text not null,
                    created_at text not null,
                    event_id text,
                    sequence integer,
                    turn_id text,
                    step_id text,
                    tool_call_id text,
                    approval_id text,
                    source text,
                    schema_version integer
                )
                """
            )
            columns = {row[1] for row in conn.execute("pragma table_info(events)")}
            if "session_id" not in columns and "thread_id" in columns:
                conn.execute("alter table events rename column thread_id to session_id")
                columns.remove("thread_id")
                columns.add("session_id")
            additions = {
                "event_id": "text",
                "sequence": "integer",
                "turn_id": "text",
                "step_id": "text",
                "tool_call_id": "text",
                "approval_id": "text",
                "source": "text",
                "schema_version": "integer",
            }
            for column, column_type in additions.items():
                if column not in columns:
                    conn.execute(f"alter table events add column {column} {column_type}")
            migration_rows = conn.execute(
                "select distinct session_id from events "
                "where event_id is null or sequence is null or schema_version is null"
            ).fetchall()
            for (session_id,) in migration_rows:
                conn.execute(
                    "update events set sequence = null where session_id = ?", (session_id,)
                )
                legacy_rows = conn.execute(
                    "select id, payload, event_id, source, schema_version from events "
                    "where session_id = ? order by id",
                    (session_id,),
                ).fetchall()
                for sequence, row in enumerate(legacy_rows, start=1):
                    row_id, raw_payload, event_id, source, version = row
                    try:
                        payload = json.loads(raw_payload)
                    except json.JSONDecodeError as exc:
                        raise ValueError(f"corrupt legacy runtime event at row {row_id}") from exc
                    if not isinstance(payload, dict):
                        raise ValueError(
                            f"legacy runtime event payload at row {row_id} is not an object"
                        )
                    payload_json = json.dumps(
                        self.sanitize_payload(payload),
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    )
                    conn.execute(
                        "update events set payload = ?, event_id = ?, sequence = ?, source = ?, "
                        "schema_version = ? where id = ?",
                        (
                            payload_json,
                            event_id or f"evt_{uuid.uuid4().hex}",
                            sequence,
                            source or "legacy-runtime",
                            version or 1,
                            row_id,
                        ),
                    )
            conn.execute(
                "create unique index if not exists idx_events_session_sequence "
                "on events(session_id, sequence) where sequence is not null"
            )
            conn.execute(
                "create unique index if not exists idx_events_event_id "
                "on events(event_id) where event_id is not null"
            )
            needs_metadata_backfill = (
                conn.execute(
                    "select 1 from sessions where updated_at is null "
                    "or last_sequence is null or status is null limit 1"
                ).fetchone()
                or conn.execute(
                    "select 1 from events left join sessions on sessions.id = events.session_id "
                    "where sessions.id is null limit 1"
                ).fetchone()
            )
            if needs_metadata_backfill:
                conn.execute(
                    "insert or ignore into sessions(id, created_at) "
                    "select session_id, min(created_at) from events group by session_id"
                )
                conn.execute(
                    "with event_stats as ("
                    "  select session_id, max(sequence) as last_sequence, "
                    "         max(case when type != 'session/title_updated' then created_at end) "
                    "           as updated_at "
                    "  from events group by session_id"
                    "), last_events as ("
                    "  select e.session_id, e.type from events e join event_stats s "
                    "    on s.session_id = e.session_id and "
                    "s.last_sequence = e.sequence"
                    ") update sessions set updated_at = ("
                    "  select updated_at from event_stats "
                    "where event_stats.session_id = sessions.id"
                    "), last_sequence = ("
                    "  select last_sequence from event_stats "
                    "where event_stats.session_id = sessions.id"
                    "), status = case ("
                    "  select type from last_events "
                    "where last_events.session_id = sessions.id"
                    ") when 'turn/started' then 'running' "
                    "when 'turn/completed' then 'completed' "
                    "when 'turn/failed' then 'failed' "
                    "when 'turn/canceled' then 'canceled' "
                    "else coalesce(sessions.status, 'created') end "
                    "where exists (select 1 from event_stats "
                    "where event_stats.session_id = sessions.id)"
                )
                conn.execute(
                    "update sessions set updated_at = coalesce(updated_at, created_at), "
                    "last_sequence = coalesce(last_sequence, 0), "
                    "status = coalesce(status, 'created'), "
                    "kind = coalesce(kind, 'root')"
                )
            conn.execute("update sessions set kind = 'root' where kind is null")
            # The retired catalog event is read here only to backfill old session metadata.
            child_rows = conn.execute(
                "select session_id, payload from events where type in "
                "('agent/subagent_descriptor', 'agent/child_started', 'agent/subagent_catalog')"
            ).fetchall()
            for session_id, raw_payload in child_rows:
                try:
                    payload = json.loads(raw_payload)
                except json.JSONDecodeError:
                    continue
                if not isinstance(payload, dict):
                    continue
                child_session_id = str(payload.get("child_session_id") or "")
                parent_session_id = str(payload.get("parent_session_id") or session_id or "")
                if not child_session_id or not parent_session_id:
                    continue
                conn.execute(
                    "update sessions set kind = 'child', parent_session_id = ? where id = ?",
                    (parent_session_id, child_session_id),
                )
            conn.execute(
                "create index if not exists idx_events_session_turn_sequence "
                "on events(session_id, turn_id, sequence)"
            )
            conn.execute(
                "create index if not exists idx_events_turn_starts "
                "on events(session_id, sequence) where type = 'turn/started'"
            )
            conn.execute(
                "create index if not exists idx_sessions_updated_at "
                "on sessions(updated_at desc, id desc)"
            )
            conn.execute(
                "create index if not exists idx_sessions_parent "
                "on sessions(parent_session_id) where kind = 'child'"
            )
            conn.execute(
                "create table if not exists session_leases ("
                "session_id text primary key, owner_token text not null unique, "
                "attempt_id text not null, base_sequence integer not null, "
                "surface_hash text not null, expires_at text not null, runtime_port integer)"
            )
            if "runtime_port" not in {
                row[1] for row in conn.execute("pragma table_info(session_leases)")
            }:
                conn.execute("alter table session_leases add column runtime_port integer")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.execute("pragma foreign_keys = on")
        return conn


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _is_expired(value: Any, now: datetime) -> bool:
    try:
        return datetime.fromisoformat(str(value)) <= now
    except (TypeError, ValueError):
        return True
