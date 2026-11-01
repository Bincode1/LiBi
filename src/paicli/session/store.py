from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from paicli.policy.redaction import Redactor
from paicli.session.models import CompactionLease, SessionEvent, SessionState, SessionSummary


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


class SessionStore:
    """Transactional, append-only event storage backed by the Runtime SQLite database."""

    def __init__(self, db_path: str | Path, *, sensitive_values: tuple[str, ...] = ()):
        self.db_path = Path(db_path).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.redactor = Redactor(sensitive_values)
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
            conn.execute(
                "insert or ignore into sessions"
                "(id, created_at, updated_at, last_sequence, status, kind, parent_session_id) "
                "values (?, ?, ?, 0, ?, ?, ?)",
                (identity, created_at, created_at, "created", kind, parent_session_id),
            )
        return identity

    def session_exists(self, session_id: str) -> bool:
        """Return whether a persisted session can be resumed by its identifier."""
        if not session_id.strip():
            return False
        with closing(self._connect()) as conn:
            row = conn.execute(
                "select 1 from sessions where id = ? "
                "union select 1 from events where session_id = ? limit 1",
                (session_id, session_id),
            ).fetchone()
        return row is not None

    def list_sessions(
        self, *, limit: int = 100, include_children: bool = False
    ) -> list[SessionSummary]:
        """Return indexed session metadata without loading event payloads."""
        if limit < 1:
            raise ValueError("session list limit must be at least 1")
        with closing(self._connect()) as conn:
            where = "" if include_children else "where kind = 'root'"
            rows = conn.execute(
                "select id, created_at, updated_at, last_sequence, status, kind, parent_session_id "
                f"from sessions {where} order by updated_at desc, id desc limit ?",
                (limit,),
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
            )
            for (
                session_id,
                created_at,
                updated_at,
                last_sequence,
                status,
                kind,
                parent_session_id,
            ) in rows
        ]

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
        cleaned = self.sanitize_payload(payload)
        payload_json = json.dumps(
            cleaned, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        event_id = f"evt_{uuid.uuid4().hex}"
        created_at = _now()
        conn = self._connect()
        try:
            conn.execute("begin immediate")
            conn.execute(
                "insert or ignore into sessions"
                "(id, created_at, updated_at, last_sequence, status, kind) "
                "values (?, ?, ?, 0, ?, ?)",
                (session_id, created_at, created_at, "created", "root"),
            )
            self._assert_write_allowed(conn, session_id, event_type, lease_token)
            sequence = int(
                conn.execute(
                    "select coalesce(max(sequence), 0) + 1 from events where session_id = ?",
                    (session_id,),
                ).fetchone()[0]
            )
            conn.execute(
                """
                insert into events(
                    session_id, type, payload, created_at, event_id, sequence, turn_id,
                    step_id, tool_call_id, approval_id, source, schema_version
                ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
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
                    schema_version,
                ),
            )
            conn.execute(
                "update sessions set updated_at = ?, last_sequence = ?, "
                "status = case ? "
                "when 'turn/started' then 'running' "
                "when 'turn/completed' then 'completed' "
                "when 'turn/failed' then 'failed' "
                "when 'turn/canceled' then 'canceled' "
                "else coalesce(status, 'created') end where id = ?",
                (created_at, sequence, event_type, session_id),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
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
            if current_sequence != base_sequence:
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
                "surface_hash, expires_at) values (?, ?, ?, ?, ?, ?)",
                (session_id, owner_token, attempt_id, base_sequence, surface_hash, expires_at),
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

    def events(self, session_id: str, *, after_sequence: int = 0) -> list[SessionEvent]:
        if after_sequence < 0:
            raise ValueError("event cursor cannot be negative")
        with closing(self._connect()) as conn, conn:
            rows = conn.execute(
                """
                select event_id, sequence, session_id, turn_id, step_id, tool_call_id,
                       approval_id, type, source, payload, created_at, schema_version
                from events
                where session_id = ? and sequence > ? and schema_version is not null
                order by sequence
                """,
                (session_id, after_sequence),
            ).fetchall()
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
        events = self.events(session_id)
        turn_id: str | None = None
        step_id: str | None = None
        status = "created"
        compaction_id: str | None = None
        for event in events:
            if event.event_type == "turn/started":
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
            session_id=session_id,
            status=status,
            last_sequence=events[-1].sequence if events else 0,
            active_turn_id=turn_id,
            active_step_id=step_id,
            latest_compaction_id=compaction_id,
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
                None,
                None,
                source,
                1,
            ),
        )
        conn.execute(
            "update sessions set updated_at = ?, last_sequence = ?, "
            "status = case ? "
            "when 'turn/started' then 'running' "
            "when 'turn/completed' then 'completed' "
            "when 'turn/failed' then 'failed' "
            "when 'turn/canceled' then 'canceled' "
            "else coalesce(status, 'created') end where id = ?",
            (created_at, sequence, event_type, session_id),
        )
        return event_id, sequence

    def _ensure_schema(self) -> None:
        with closing(self._connect()) as conn, conn:
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
            needs_metadata_backfill = conn.execute(
                "select 1 from sessions where updated_at is null "
                "or last_sequence is null or status is null limit 1"
            ).fetchone() or conn.execute(
                "select 1 from events left join sessions on sessions.id = events.session_id "
                "where sessions.id is null limit 1"
            ).fetchone()
            if needs_metadata_backfill:
                conn.execute(
                    "insert or ignore into sessions(id, created_at) "
                    "select session_id, min(created_at) from events group by session_id"
                )
                conn.execute(
                    "with event_stats as ("
                    "  select session_id, max(sequence) as last_sequence, "
                    "         max(created_at) as updated_at "
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
            conn.execute("update sessions set kind = coalesce(kind, 'root')")
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
                "create index if not exists idx_sessions_updated_at "
                "on sessions(updated_at desc, id desc)"
            )
            conn.execute(
                "create table if not exists session_leases ("
                "session_id text primary key, owner_token text not null unique, "
                "attempt_id text not null, base_sequence integer not null, "
                "surface_hash text not null, expires_at text not null)"
            )

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
