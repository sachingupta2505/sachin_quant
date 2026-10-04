"""
Event-Driven Blackboard Message Bus
Implements Blackboard Pattern using SQLite (system_bus.db) with WAL concurrency.
Table: bus_events
Columns: id, timestamp, source_agent, target_agent, topic, payload (JSON), status
Status enum: PENDING, PROCESSING, COMPLETED, VETOED, FAILED
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
SYSTEM_BUS_DB = Path("system_bus.db")


class EventStatus(str, Enum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    VETOED = "VETOED"
    FAILED = "FAILED"


@dataclass
class BusEvent:
    id: int
    timestamp: str
    source_agent: str
    target_agent: str
    topic: str
    payload: dict[str, Any]
    status: str

    def get(self, key: str, default: Any = None) -> Any:
        return self.payload.get(key, default)


class SystemBus:
    """
    Event-driven blackboard bus implemented over SQLite.
    Guarantees ACID transactions, concurrent multi-process access (WAL mode),
    and reliable event dispatch across autonomous agents.
    """

    def __init__(self, db_path: str | Path = SYSTEM_BUS_DB):
        self.db_path = Path(db_path)
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=15.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA busy_timeout = 5000;")
        return conn

    def _init_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._get_connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS bus_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    source_agent TEXT NOT NULL,
                    target_agent TEXT NOT NULL,
                    topic TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('PENDING', 'PROCESSING', 'COMPLETED', 'VETOED', 'FAILED'))
                );
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_bus_events_lookup
                ON bus_events(topic, target_agent, status);
                """
            )
            conn.commit()

    def publish(
        self,
        topic: str,
        source: str,
        payload: dict[str, Any],
        target: str = "ALL",
        status: EventStatus = EventStatus.PENDING,
    ) -> int:
        """
        Publishes an event to the blackboard.
        Returns the unique event id.
        """
        now_iso = datetime.now(IST).isoformat()
        payload_json = json.dumps(payload, default=str)

        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO bus_events (timestamp, source_agent, target_agent, topic, payload, status)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (now_iso, source, target, topic, payload_json, status.value),
            )
            conn.commit()
            return cursor.lastrowid

    def consume(
        self,
        topic: str,
        target: str,
        auto_ack: bool = True,
    ) -> list[BusEvent]:
        """
        Consumes all PENDING events matching the specified topic and target (or ALL).
        If auto_ack is True, atomically marks them as PROCESSING so crashes can be detected.
        Consuming agents must explicitly call update_status(event_id, EventStatus.COMPLETED)
        or EventStatus.VETOED / EventStatus.FAILED once processing completes.
        """
        events: list[BusEvent] = []

        with self._get_connection() as conn:
            rows = conn.execute(
                """
                SELECT id, timestamp, source_agent, target_agent, topic, payload, status
                FROM bus_events
                WHERE topic = ? 
                  AND (target_agent = ? OR target_agent = 'ALL')
                  AND status = 'PENDING'
                ORDER BY id ASC
                """,
                (topic, target),
            ).fetchall()

            if not rows:
                return []

            event_ids = []
            for r in rows:
                event_ids.append(r["id"])
                events.append(
                    BusEvent(
                        id=r["id"],
                        timestamp=r["timestamp"],
                        source_agent=r["source_agent"],
                        target_agent=r["target_agent"],
                        topic=r["topic"],
                        payload=json.loads(r["payload"]),
                        status=EventStatus.PROCESSING.value if auto_ack else r["status"],
                    )
                )

            if auto_ack and event_ids:
                placeholders = ",".join("?" for _ in event_ids)
                conn.execute(
                    f"UPDATE bus_events SET status = 'PROCESSING' WHERE id IN ({placeholders})",
                    event_ids,
                )
                conn.commit()

        return events

    def update_status(self, event_id: int, new_status: EventStatus) -> None:
        """Updates the lifecycle status of an event."""
        with self._get_connection() as conn:
            conn.execute(
                "UPDATE bus_events SET status = ? WHERE id = ?",
                (new_status.value, event_id),
            )
            conn.commit()

    def get_event(self, event_id: int) -> Optional[BusEvent]:
        with self._get_connection() as conn:
            r = conn.execute("SELECT * FROM bus_events WHERE id = ?", (event_id,)).fetchone()
            if not r:
                return None
            return BusEvent(
                id=r["id"],
                timestamp=r["timestamp"],
                source_agent=r["source_agent"],
                target_agent=r["target_agent"],
                topic=r["topic"],
                payload=json.loads(r["payload"]),
                status=r["status"],
            )

    def get_recent_events(self, limit: int = 20) -> list[BusEvent]:
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM bus_events ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
            return [
                BusEvent(
                    id=r["id"],
                    timestamp=r["timestamp"],
                    source_agent=r["source_agent"],
                    target_agent=r["target_agent"],
                    topic=r["topic"],
                    payload=json.loads(r["payload"]),
                    status=r["status"],
                )
                for r in reversed(rows)
            ]


# Global singleton instance convenience
bus = SystemBus()
