"""
SQLite Multiprocess Message Bus for Decoupled Agents
Enables inter-process communication between independent agent processes via bus.db.
Configured with WAL mode and busy timeout for concurrent multi-process access.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
BUS_DB_PATH = Path("bus.db")


@dataclass
class BusMessage:
    id: int
    msg_id: str
    sender: str
    recipient: str
    msg_type: str
    payload: dict[str, Any]
    timestamp: str


class SQLiteMessageBus:
    def __init__(self, db_path: str | Path = BUS_DB_PATH):
        self.db_path = Path(db_path)
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        # Enable Write-Ahead Logging (WAL) for high-concurrency multi-process read/write
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA busy_timeout = 5000;")
        return conn

    def _init_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._get_connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    msg_id TEXT UNIQUE NOT NULL,
                    sender TEXT NOT NULL,
                    recipient TEXT NOT NULL,
                    msg_type TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    processed INTEGER DEFAULT 0
                );
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_messages_recipient_processed 
                ON messages(recipient, processed);
                """
            )
            conn.commit()

    def publish(
        self,
        sender: str,
        recipient: str,
        msg_type: str,
        payload: dict[str, Any],
        msg_id: Optional[str] = None,
    ) -> str:
        """Publishes an event message to the bus for a target agent or broadcast."""
        mid = msg_id or f"BUS-{uuid.uuid4().hex[:8].upper()}"
        now_iso = datetime.now(IST).isoformat()
        payload_json = json.dumps(payload, default=str)

        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO messages (msg_id, sender, recipient, msg_type, payload, timestamp, processed)
                VALUES (?, ?, ?, ?, ?, ?, 0)
                """,
                (mid, sender, recipient, msg_type, payload_json, now_iso),
            )
            conn.commit()
        return mid

    def poll(self, recipient: str, mark_processed: bool = True) -> list[BusMessage]:
        """Polls for unprocessed messages targeted to `recipient` or 'ALL'."""
        results: list[BusMessage] = []
        with self._get_connection() as conn:
            rows = conn.execute(
                """
                SELECT id, msg_id, sender, recipient, msg_type, payload, timestamp
                FROM messages
                WHERE (recipient = ? OR recipient = 'ALL') AND processed = 0
                ORDER BY id ASC
                """,
                (recipient,),
            ).fetchall()

            if not rows:
                return []

            ids_to_mark = []
            for r in rows:
                ids_to_mark.append(r["id"])
                results.append(
                    BusMessage(
                        id=r["id"],
                        msg_id=r["msg_id"],
                        sender=r["sender"],
                        recipient=r["recipient"],
                        msg_type=r["msg_type"],
                        payload=json.loads(r["payload"]),
                        timestamp=r["timestamp"],
                    )
                )

            if mark_processed and ids_to_mark:
                placeholders = ",".join("?" for _ in ids_to_mark)
                conn.execute(
                    f"UPDATE messages SET processed = 1 WHERE id IN ({placeholders})",
                    ids_to_mark,
                )
                conn.commit()

        return results
