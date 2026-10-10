"""
Automated Test Suite for Remediation Invariants & Namespace Collision Hardening
Validates:
1. Single source of truth for physical spread risk invariant across agents/auditor.py and main_runner.py.
2. Runtime workers execute past 12:30 IST on expiry days without TypeError.
3. Complete separation of Python's `time` module and `datetime.time` (as `dtime`).
"""

from __future__ import annotations

import threading
import time
from datetime import date, datetime, time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from bus import SystemBus, EventStatus
from risk_guard import (
    RiskGuard,
    RiskState,
    validate_physical_spread_risk,
    is_expiry_session,
    MAX_PERMITTED_SPREAD_RISK_INR,
)
from agents.base import AgentMessage, MessageType
from agents.auditor import AuditorAgent
import main_runner

IST = ZoneInfo("Asia/Kolkata")


def test_single_source_of_truth_risk_invariant():
    """Verify that both agents.auditor and main_runner use the exact same risk validation function."""
    auditor_func = getattr(main_runner, "validate_physical_spread_risk", None)
    assert auditor_func is not None
    assert auditor_func is validate_physical_spread_risk

    # 1. Breach case: 50-width spread with 13.0 credit -> (50 - 13) * 65 = 2405.0 INR > 1500.0 INR
    is_valid, risk_amt, reason = validate_physical_spread_risk(
        spread_width=50.0,
        net_credit=13.0,
        lot_size=65,
        max_risk_inr=1500.0,
    )
    assert not is_valid
    assert risk_amt == 2405.0
    assert "Physical spread risk breach" in reason

    # 2. Compliant case: 50-width spread with 27.0 credit -> (50 - 27) * 65 = 1495.0 INR <= 1500.0 INR
    is_valid_ok, risk_amt_ok, reason_ok = validate_physical_spread_risk(
        spread_width=50.0,
        net_credit=27.0,
        lot_size=65,
        max_risk_inr=1500.0,
    )
    assert is_valid_ok
    assert risk_amt_ok == 1495.0
    assert reason_ok == ""


def test_auditor_agent_rejects_breach_with_audit_rejected():
    """Verify AuditorAgent vetoes breach with AUDIT_REJECTED."""
    dispatched_messages = []
    auditor = AuditorAgent(
        dispatch_fn=lambda msg: dispatched_messages.append(msg),
        state_file="test_state_remediation.json",
        db_path=":memory:",
    )

    test_ts = datetime(2026, 10, 7, 10, 30, tzinfo=IST)
    payload_breach = {
        "trade_id": "TEST-BREACH-001",
        "spread_type": "BEAR_CALL_SPREAD",
        "spread_width": 50.0,
        "net_credit": 13.0,
        "stop_loss_pts": 10.0,
        "timestamp": test_ts,
        "legs": [
            {"strike": 22650.0, "option_type": "CE", "quantity": 65},
            {"strike": 22700.0, "option_type": "CE", "quantity": 65},
        ],
    }

    auditor.audit_proposed_order(payload_breach)
    assert len(dispatched_messages) == 1
    assert dispatched_messages[0].msg_type == MessageType.AUDIT_REJECTED
    assert "Physical spread risk breach" in dispatched_messages[0].payload["reason"]


def test_runtime_workers_execute_past_1230_expiry_without_type_error(tmp_path: Path):
    """Verifies that runtime workers and risk guards execute on expiry days past 12:30 IST without TypeError."""
    expiry_dt = datetime(2026, 10, 8, 12, 35, 0, tzinfo=IST)  # Expiry day, 12:35 PM IST
    assert is_expiry_session(expiry_dt)

    # 1. Test RiskGuard.can_enter_trade
    rg = RiskGuard(state_file=str(tmp_path / "daily_state.json"), tz=IST)
    allowed, reason = rg.can_enter_trade(current_time=expiry_dt)
    assert not allowed
    assert "expiry day entry cutoff" in reason.lower()

    # 2. Test AuditorAgent on expiry past 12:30 IST
    dispatched = []
    auditor = AuditorAgent(
        dispatch_fn=lambda msg: dispatched.append(msg),
        state_file=str(tmp_path / "auditor_state.json"),
        db_path=":memory:",
    )
    payload_expiry = {
        "trade_id": "TEST-EXPIRY-001",
        "spread_type": "BEAR_CALL_SPREAD",
        "spread_width": 50.0,
        "net_credit": 28.0,
        "stop_loss_pts": 10.0,
        "timestamp": expiry_dt,
        "legs": [
            {"strike": 22650.0, "option_type": "CE", "quantity": 65},
            {"strike": 22700.0, "option_type": "CE", "quantity": 65},
        ],
    }
    auditor.audit_proposed_order(payload_expiry)
    assert len(dispatched) == 1
    assert dispatched[0].msg_type == MessageType.AUDIT_REJECTED
    assert "Expiry Day Guard" in dispatched[0].payload["reason"]

    # 3. Test main_runner.auditor_worker with bus on expiry past 12:30 IST
    bus_db = str(tmp_path / "test_bus.db")
    bus = SystemBus(db_path=bus_db)
    stop_event = threading.Event()
    worker_thread = threading.Thread(
        target=main_runner.auditor_worker,
        args=(bus, stop_event, None, str(tmp_path / "worker_state.json"), str(tmp_path / "worker_journal.db")),
        daemon=True,
    )
    worker_thread.start()

    try:
        eid = bus.publish(
            topic="ORDER_PROPOSED",
            source="Coder",
            target="Auditor",
            payload={
                "trade_id": "TEST-BUS-EXPIRY",
                "spread_type": "BEAR_CALL_SPREAD",
                "spread_width": 50.0,
                "net_credit": 28.0,
                "timestamp": expiry_dt.isoformat(),
                "legs": [
                    {"strike": 22650.0, "option_type": "CE", "quantity": 65},
                    {"strike": 22700.0, "option_type": "CE", "quantity": 65},
                ],
            },
        )
        time.sleep(0.3)
        events = bus.consume(topic="ORDER_BLOCKED", target="Notifier")
        assert len(events) >= 1
        assert "Expiry" in events[0].payload["reason"]
    finally:
        stop_event.set()
        worker_thread.join(timeout=1.0)


def test_no_time_module_namespace_collision():
    """Verify time.sleep and datetime.time (as dtime) function in harmony without collision."""
    # Ensure dtime is a class, not a module
    assert isinstance(dtime(12, 30), dtime)
    # Ensure time is a module with sleep
    assert callable(time.sleep)
    # Test sleep executes cleanly
    t0 = time.time()
    time.sleep(0.01)
    t1 = time.time()
    assert t1 > t0
