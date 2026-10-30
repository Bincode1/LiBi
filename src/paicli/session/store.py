from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from paicli.policy.redaction import Redactor
from paicli.session.models import SessionEvent, SessionState


class SessionStore:
    """Transactional, append-only event storage backed by the Runtime SQLite database."""

    def __init__(self, db_path: str | Path, *, sensitive_values: tuple[str, ...] = ()):
        self.db_path = Path(db_path).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.redactor = Redactor(sensitive_values)
        self._ensure_schema()

    def create_session(self, session_id: str | None = None) -> str:
        identity = session_id or f"session_{uuid.uuid4().hex}"
        with closing(self._connect()) as conn, conn:
            conn.execute(
                "insert or ignore into sessions(id, created_at) values (?, ?)",
                (identity, _now()),
            )
        return identity

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
                "insert or ignore into sessions(id, created_at) values (?, ?)",
                (session_id, created_at),
            )
            sequence = int(
                conn.execute(
                    "select coalesce(max(sequence), 0) + 1 from events where thread_id = ?",
                    (session_id,),
                ).fetchone()[0]
            )
            conn.execute(
                """
                insert into events(
                    thread_id, type, payload, created_at, event_id, sequence, turn_id,
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

    def events(self, session_id: str, *, after_sequence: int = 0) -> list[SessionEvent]:
        if after_sequence < 0:
            raise ValueError("event cursor cannot be negative")
        with closing(self._connect()) as conn, conn:
            rows = conn.execute(
                """
                select event_id, sequence, thread_id, turn_id, step_id, tool_call_id,
                       approval_id, type, source, payload, created_at, schema_version
                from events
                where thread_id = ? and sequence > ? and schema_version is not null
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

    def _ensure_schema(self) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """
                create table if not exists sessions (
                    id text primary key,
                    created_at text not null
                )
                """
            )
            conn.execute(
                """
                create table if not exists events (
                    id integer primary key autoincrement,
                    thread_id text not null,
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
                "select distinct thread_id from events "
                "where event_id is null or sequence is null or schema_version is null"
            ).fetchall()
            for (thread_id,) in migration_rows:
                conn.execute("update events set sequence = null where thread_id = ?", (thread_id,))
                thread_rows = conn.execute(
                    "select id, payload, event_id, source, schema_version from events "
                    "where thread_id = ? order by id",
                    (thread_id,),
                ).fetchall()
                for sequence, row in enumerate(thread_rows, start=1):
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
                "on events(thread_id, sequence) where sequence is not null"
            )
            conn.execute(
                "create unique index if not exists idx_events_event_id "
                "on events(event_id) where event_id is not null"
            )
            conn.execute(
                "create index if not exists idx_events_session_turn_sequence "
                "on events(thread_id, turn_id, sequence)"
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.execute("pragma foreign_keys = on")
        return conn


def _now() -> str:
    return datetime.now(UTC).isoformat()
