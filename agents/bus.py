"""
agents/bus.py - DEPRECATED ALIAS
This module is deprecated in favor of root bus.py (SystemBus over system_bus.db).
All agents and new code must strictly import SystemBus from root bus.py to prevent
split-brain SQLite databases (bus.db vs system_bus.db).
"""

from __future__ import annotations

import json
import sys
import warnings
from pathlib import Path
from typing import Any, Optional

# Add project root to sys.path
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from bus import BusEvent, EventStatus, SystemBus, bus

warnings.warn(
    "agents.bus is deprecated. Import SystemBus directly from root bus.py instead.",
    DeprecationWarning,
    stacklevel=2,
)


class SQLiteMessageBus:
    """Deprecated compatibility adapter routing over SystemBus."""

    def __init__(self, db_path: str | Path = "system_bus.db"):
        self.bus = SystemBus(db_path=db_path)
        self.db_path = self.bus.db_path

    def publish(
        self,
        sender: str,
        recipient: str,
        msg_type: str,
        payload: dict[str, Any],
    ) -> str:
        eid = self.bus.publish(
            topic=msg_type,
            source=sender,
            target=recipient,
            payload=payload,
        )
        return f"BUS-{eid}"

    def poll(self, recipient: str, limit: int = 50) -> list[Any]:
        with self.bus._get_connection() as conn:
            rows = conn.execute(
                """
                SELECT id, timestamp, source_agent, target_agent, topic, payload, status
                FROM bus_events
                WHERE (target_agent = ? OR target_agent = 'ALL')
                  AND status = 'PENDING'
                ORDER BY id ASC
                LIMIT ?
                """,
                (recipient, limit),
            ).fetchall()

            if not rows:
                return []

            results = []
            ids = []
            for r in rows:
                ids.append(r["id"])

                class LegacyMessage:
                    def __init__(self, row):
                        self.id = row["id"]
                        self.msg_id = f"BUS-{row['id']}"
                        self.sender = row["source_agent"]
                        self.recipient = row["target_agent"]
                        self.msg_type = row["topic"]
                        self.payload = json.loads(r["payload"]) if isinstance(r["payload"], str) else r["payload"]
                        self.timestamp = row["timestamp"]

                results.append(LegacyMessage(r))

            placeholders = ",".join("?" for _ in ids)
            conn.execute(
                f"UPDATE bus_events SET status = 'COMPLETED' WHERE id IN ({placeholders})",
                ids,
            )
            conn.commit()
            return results


__all__ = ["SystemBus", "EventStatus", "BusEvent", "bus", "SQLiteMessageBus"]
