"""
Unit Tests for SQLite Message Bus (Multiprocess IPC)
Verifies inter-process message publishing, polling, and recipient isolation.
"""

from __future__ import annotations

from pathlib import Path
import pytest

from agents.bus import SQLiteMessageBus


@pytest.fixture
def temp_bus_db(tmp_path: Path) -> Path:
    return tmp_path / "test_bus.db"


def test_bus_publish_and_poll(temp_bus_db: Path):
    bus = SQLiteMessageBus(db_path=temp_bus_db)

    # Publish message from Architect to Coder
    mid = bus.publish(
        sender="Architect",
        recipient="Coder",
        msg_type="STRATEGY_SIGNAL",
        payload={"spot": 25000.0, "signal": "BULLISH_REJECTION"},
    )
    assert mid.startswith("BUS-")

    # Auditor polls -> should find 0 messages
    auditor_msgs = bus.poll(recipient="Auditor")
    assert len(auditor_msgs) == 0

    # Coder polls -> should find 1 message
    coder_msgs = bus.poll(recipient="Coder")
    assert len(coder_msgs) == 1
    msg = coder_msgs[0]
    assert msg.sender == "Architect"
    assert msg.recipient == "Coder"
    assert msg.msg_type == "STRATEGY_SIGNAL"
    assert msg.payload["spot"] == 25000.0

    # Coder polls again -> already processed, should find 0 messages
    coder_msgs_second = bus.poll(recipient="Coder")
    assert len(coder_msgs_second) == 0


def test_bus_broadcast_message(temp_bus_db: Path):
    bus = SQLiteMessageBus(db_path=temp_bus_db)

    bus.publish(
        sender="DevOps",
        recipient="ALL",
        msg_type="HEARTBEAT",
        payload={"status": "OK"},
    )

    # Any agent polling should receive the broadcast message
    msgs = bus.poll(recipient="Auditor")
    assert len(msgs) == 1
    assert msgs[0].msg_type == "HEARTBEAT"
