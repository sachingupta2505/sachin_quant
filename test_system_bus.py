"""
Unit Tests for Event-Driven Blackboard Pattern (bus.py)
Validates system_bus.db schema, publish/consume semantics, and status lifecycles.
"""

from __future__ import annotations

from pathlib import Path
import pytest

from bus import EventStatus, SystemBus


@pytest.fixture
def temp_bus(tmp_path: Path) -> SystemBus:
    return SystemBus(db_path=tmp_path / "test_system_bus.db")


def test_bus_table_creation(temp_bus: SystemBus):
    assert temp_bus.db_path.exists()
    events = temp_bus.get_recent_events()
    assert len(events) == 0


def test_publish_and_consume_workflow(temp_bus: SystemBus):
    # Publish SIGNAL_DETECTED from Architect to Coder
    eid = temp_bus.publish(
        topic="SIGNAL_DETECTED",
        source="Architect",
        target="Coder",
        payload={"spot": 25000.0, "signal": "BULLISH_REJECTION"},
    )
    assert eid > 0

    # Wrong topic or target shouldn't consume
    assert len(temp_bus.consume("ORDER_PROPOSED", target="Coder")) == 0
    assert len(temp_bus.consume("SIGNAL_DETECTED", target="Auditor")) == 0

    # Correct topic & target consumes and marks COMPLETED
    consumed = temp_bus.consume("SIGNAL_DETECTED", target="Coder")
    assert len(consumed) == 1
    event = consumed[0]
    assert event.id == eid
    assert event.source_agent == "Architect"
    assert event.target_agent == "Coder"
    assert event.topic == "SIGNAL_DETECTED"
    assert event.payload["spot"] == 25000.0

    # Second consume should be empty (auto-acked to COMPLETED)
    assert len(temp_bus.consume("SIGNAL_DETECTED", target="Coder")) == 0

    # Verify event record in DB is marked COMPLETED
    db_event = temp_bus.get_event(eid)
    assert db_event is not None
    assert db_event.status == "COMPLETED"


def test_event_status_veto(temp_bus: SystemBus):
    eid = temp_bus.publish(
        topic="ORDER_PROPOSED",
        source="Coder",
        target="Auditor",
        payload={"trade_id": "T1", "max_risk": 2000.0},
    )

    # Consume without auto-ack
    events = temp_bus.consume("ORDER_PROPOSED", target="Auditor", auto_ack=False)
    assert len(events) == 1

    # Auditor marks VETOED
    temp_bus.update_status(eid, EventStatus.VETOED)
    updated = temp_bus.get_event(eid)
    assert updated.status == EventStatus.VETOED.value
