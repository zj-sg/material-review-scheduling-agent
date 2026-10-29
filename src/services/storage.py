"""Two SQLite tables: formal occupancy and one current draft per request."""

import json
import sqlite3
from contextlib import closing, contextmanager
from datetime import datetime
from pathlib import Path

from src.schemas.common import utc
from src.schemas.request import ScheduleRequest
from src.schemas.scheduling import ScheduledSlot, TimeWindow

from .schedule_service import overlaps


def request_key(request: ScheduleRequest) -> str:
    def encode(value):
        if isinstance(value, datetime):
            return utc(value).isoformat()
        raise TypeError(f"unsupported request value: {type(value).__name__}")

    return json.dumps(
        request.model_dump(), default=encode, sort_keys=True,
        ensure_ascii=False, separators=(",", ":"),
    )


class SQLiteStore:
    def __init__(self, path: Path):
        self.path = Path(path).resolve()

    @property
    def reference(self) -> str:
        return f"sqlite:{self.path.as_posix()}"

    @contextmanager
    def transaction(self, *, initial_schedule: list[ScheduledSlot] | None = None):
        if initial_schedule is not None:
            initial_schedule = [ScheduledSlot.model_validate(slot.model_dump()) for slot in initial_schedule]
            if len({slot.schedule_id for slot in initial_schedule}) != len(initial_schedule):
                raise ValueError("schedule_id values must be unique")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(self.path, timeout=5, isolation_level=None)) as connection:
            connection.row_factory = sqlite3.Row
            try:
                connection.execute("BEGIN IMMEDIATE")
                schedule_exists = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='scheduled_slots'"
                ).fetchone() if initial_schedule is not None else True
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS scheduled_slots ("
                    "schedule_id TEXT PRIMARY KEY, channel TEXT NOT NULL, "
                    "start_at TEXT NOT NULL, end_at TEXT NOT NULL)"
                )
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS drafts ("
                    "draft_id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE, "
                    "request_json TEXT NOT NULL, rules_json TEXT NOT NULL, report_json TEXT NOT NULL)"
                )
                # HTTP startup seeds a new table once; restarts preserve authoritative occupancy.
                if not schedule_exists and initial_schedule is not None:
                    connection.executemany(
                        "INSERT INTO scheduled_slots VALUES (?, ?, ?, ?)",
                        [(slot.schedule_id, slot.channel, slot.start_at.isoformat(), slot.end_at.isoformat())
                         for slot in initial_schedule],
                    )
                yield connection
                if connection.in_transaction:
                    connection.commit()
            except Exception:
                connection.rollback()
                raise

    def replace_schedule(self, slots: list[ScheduledSlot]) -> None:
        checked = [ScheduledSlot.model_validate(slot.model_dump()) for slot in slots]
        if len({slot.schedule_id for slot in checked}) != len(checked):
            raise ValueError("schedule_id values must be unique")
        with self.transaction() as connection:
            connection.execute("DELETE FROM scheduled_slots")
            connection.executemany(
                "INSERT INTO scheduled_slots VALUES (?, ?, ?, ?)",
                [
                    (slot.schedule_id, slot.channel, slot.start_at.isoformat(), slot.end_at.isoformat())
                    for slot in checked
                ],
            )

    def get(self, request_id: str) -> dict | None:
        if not self.path.exists():
            return None
        with closing(sqlite3.connect(self.path, timeout=5)) as connection:
            connection.row_factory = sqlite3.Row
            if not connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='drafts'"
            ).fetchone():
                return None
            row = connection.execute(
                "SELECT * FROM drafts WHERE request_id = ?", (request_id,),
            ).fetchone()
            return dict(row) if row else None

    def count(self) -> int:
        if not self.path.exists():
            return 0
        with closing(sqlite3.connect(self.path, timeout=5)) as connection:
            if not connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='drafts'"
            ).fetchone():
                return 0
            return connection.execute("SELECT COUNT(*) FROM drafts").fetchone()[0]

    def write_draft(
        self, connection: sqlite3.Connection, *, draft_id: str, request_id: str,
        canonical_request: str, rules_json: str, report_json: str,
    ) -> None:
        connection.execute(
            "INSERT INTO drafts (draft_id, request_id, request_json, rules_json, report_json) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(request_id) DO UPDATE SET "
            "rules_json=excluded.rules_json, report_json=excluded.report_json",
            (draft_id, request_id, canonical_request, rules_json, report_json),
        )


class SQLiteScheduleSource:
    def __init__(self, connection: sqlite3.Connection, reference: str):
        self.connection = connection
        self.reference = reference + "#scheduled_slots"

    def query(self, channel: str, window: TimeWindow) -> list[ScheduledSlot]:
        rows = self.connection.execute(
            "SELECT * FROM scheduled_slots WHERE channel = ?", (channel,),
        ).fetchall()
        slots = [ScheduledSlot.model_validate(dict(row)) for row in rows]
        return sorted(
            [slot for slot in slots if overlaps(window.start, window.end, slot)],
            key=lambda slot: (utc(slot.start_at), utc(slot.end_at), slot.schedule_id),
        )
