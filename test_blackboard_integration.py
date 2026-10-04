"""
End-to-End Integration Test for Event-Driven Blackboard Pattern
Tests complete lifecycle:
Architect (SIGNAL_DETECTED)
  -> Coder (ORDER_PROPOSED)
  -> Auditor (ORDER_APPROVED / ORDER_VETOED)
  -> DevOps (ORDER_EXECUTED & POSITION_CLOSED)
  -> Auditor Journal (SQLite)
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import pytest

from bus import EventStatus, SystemBus
from audit_logger import AuditLogger
from risk_guard import RiskGuard, RiskState

IST = ZoneInfo("Asia/Kolkata")


@pytest.fixture
def clean_blackboard(tmp_path: Path):
    bus_path = tmp_path / "test_blackboard.db"
    journal_path = tmp_path / "test_journal.db"
    state_path = tmp_path / "test_state.json"
    return {
        "bus": SystemBus(db_path=bus_path),
        "journal": AuditLogger(db_path=journal_path, tz=IST),
        "risk_guard": RiskGuard(state_file=state_path, tz=IST),
    }


def test_blackboard_end_to_end_flow(clean_blackboard):
    bus: SystemBus = clean_blackboard["bus"]
    journal: AuditLogger = clean_blackboard["journal"]
    risk_guard: RiskGuard = clean_blackboard["risk_guard"]

    sim_time = datetime(2026, 10, 5, 10, 15, tzinfo=IST)

    # STEP 1: Architect detects edge and publishes SIGNAL_DETECTED
    sig_id = bus.publish(
        topic="SIGNAL_DETECTED",
        source="Architect",
        target="Coder",
        payload={
            "signal_type": "BULLISH_REJECTION",
            "spot_price": 25000.0,
            "description": "Bullish Hammer at 24950 Support",
            "timestamp": sim_time.isoformat(),
        },
    )
    assert sig_id > 0

    # STEP 2: Coder consumes SIGNAL_DETECTED and produces ORDER_PROPOSED
    signals = bus.consume(topic="SIGNAL_DETECTED", target="Coder")
    assert len(signals) == 1
    sig = signals[0]

    # Coder calculates strikes and ensures Max Risk <= 1500
    width = 50.0
    lot_size = 25
    net_credit = 18.0
    max_risk = (width * lot_size) - (net_credit * lot_size)  # 800 INR <= 1500
    assert max_risk <= 1500.0

    order_id = bus.publish(
        topic="ORDER_PROPOSED",
        source="Coder",
        target="Auditor",
        payload={
            "trade_id": "SPD-TEST-001",
            "spread_type": "BULL_PUT_SPREAD",
            "spread_width": width,
            "net_credit": net_credit,
            "max_risk_inr": max_risk,
            "timestamp": sim_time.isoformat(),
            "architect_signal": sig.payload["description"],
        },
    )
    assert order_id > 0
    bus.update_status(sig.id, EventStatus.COMPLETED)

    # STEP 3: Auditor consumes ORDER_PROPOSED, evaluates FSM, publishes ORDER_APPROVED
    orders = bus.consume(topic="ORDER_PROPOSED", target="Auditor")
    assert len(orders) == 1
    ord_event = orders[0]

    allowed, reason = risk_guard.can_enter_trade(current_time=sim_time)
    assert allowed is True

    bus.update_status(ord_event.id, EventStatus.COMPLETED)
    risk_guard.record_trade_entry("SPD-TEST-001", ord_event.payload, current_time=sim_time)
    assert risk_guard.trade_count == 1
    assert risk_guard.current_state == RiskState.IN_TRADE

    app_id = bus.publish(
        topic="ORDER_APPROVED",
        source="Auditor",
        target="DevOps",
        payload=ord_event.payload,
    )
    assert app_id > 0

    # STEP 4: DevOps consumes ORDER_APPROVED, executes order, publishes ORDER_EXECUTED
    approvals = bus.consume(topic="ORDER_APPROVED", target="DevOps")
    assert len(approvals) == 1

    exec_id = bus.publish(
        topic="ORDER_EXECUTED",
        source="DevOps",
        target="Auditor",
        payload={
            **approvals[0].payload,
            "actual_entry_price": 18.0,
            "expected_entry_price": 18.0,
            "is_paper": True,
            "timestamp": sim_time.isoformat(),
        },
    )
    assert exec_id > 0
    bus.update_status(approvals[0].id, EventStatus.COMPLETED)

    # STEP 5: Auditor consumes ORDER_EXECUTED, commits to journal
    execs = bus.consume(topic="ORDER_EXECUTED", target="Auditor")
    assert len(execs) == 1
    journal_entry = journal.log_trade_entry(
        trade_id="SPD-TEST-001",
        symbol="NIFTY",
        spread_type="BULL_PUT_SPREAD",
        expected_entry_price=18.0,
        actual_entry_price=18.0,
        is_paper=True,
        timestamp=sim_time,
    )
    assert journal_entry.trade_id == "SPD-TEST-001"
    assert journal_entry.status == "OPEN"
    bus.update_status(execs[0].id, EventStatus.COMPLETED)

    # Close trade
    risk_guard.record_trade_exit("SPD-TEST-001", realized_pnl=650.0, current_time=sim_time)
    closed = journal.log_trade_exit(
        trade_id="SPD-TEST-001",
        expected_exit_price=5.0,
        actual_exit_price=5.0,
        realized_pnl=650.0,
        timestamp=sim_time,
    )
    assert closed.status == "CLOSED"
    assert closed.realized_pnl == 650.0
    assert risk_guard.trade_count == 1


def test_blackboard_auditor_veto_on_risk_breach(clean_blackboard):
    bus: SystemBus = clean_blackboard["bus"]
    risk_guard: RiskGuard = clean_blackboard["risk_guard"]

    sim_time = datetime(2026, 10, 5, 10, 15, tzinfo=IST)

    # Order proposed with Max Risk 2400 INR > 1500 INR limit
    order_id = bus.publish(
        topic="ORDER_PROPOSED",
        source="Coder",
        target="Auditor",
        payload={
            "trade_id": "SPD-BAD-RISK",
            "spread_width": 50.0,
            "max_risk_inr": 2400.0,  # Breaches limit!
            "timestamp": sim_time.isoformat(),
        },
    )

    orders = bus.consume(topic="ORDER_PROPOSED", target="Auditor", auto_ack=False)
    assert len(orders) == 1
    ord_ev = orders[0]

    # Auditor vetoes
    assert ord_ev.payload["max_risk_inr"] > 1500.0
    bus.update_status(ord_ev.id, EventStatus.VETOED)

    veto_id = bus.publish(
        topic="ORDER_VETOED",
        source="Auditor",
        target="Coder",
        payload={"trade_id": "SPD-BAD-RISK", "reason": "Max risk > 1500 INR"},
    )

    # Verify event in DB is VETOED
    assert bus.get_event(ord_ev.id).status == EventStatus.VETOED.value

    # Coder consumes veto
    vetoes = bus.consume(topic="ORDER_VETOED", target="Coder")
    assert len(vetoes) == 1
    assert vetoes[0].payload["trade_id"] == "SPD-BAD-RISK"
    bus.update_status(vetoes[0].id, EventStatus.COMPLETED)
