"""
Unit Tests for Execution Safety & 2-Leg Incomplete Fill Guard
Module: tests/test_execution_safety.py

Verifies:
1. 2-Leg Incomplete Fill Guard in DevOpsAgent & ExecutionEngine:
   - BUY hedge leg fills first.
   - SELL short leg remains unfilled for > 5.0 seconds.
   - Pending SELL order cancelled immediately.
   - Immediate emergency MARKET square-off executed for BUY hedge leg to prevent naked exposure.
   - Critical audit event logged and dispatched:
     'LEG_FILL_TIMEOUT: Emergency square-off executed to prevent naked long hedge'
   - FSM state transitions to safe idle.
2. Auditor Lot Size Multiplier Enforcement (order.quantity % 65 == 0).
"""

import time
from datetime import datetime
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo
import pytest

from agents.auditor import AuditorAgent
from agents.base import AgentMessage, MessageType
from agents.devops import DevOpsAgent
from execution_engine import ExecutionEngine, SpreadTrade, SpreadLeg, SpreadType
from risk_guard import RiskGuard

IST = ZoneInfo("Asia/Kolkata")


def test_devops_2_leg_unfill_timeout_triggers_emergency_square_off():
    """
    Simulates a 5-second timeout on the second leg in DevOpsAgent.
    Verifies:
    1. Pending SELL limit order cancelled.
    2. Emergency MARKET square-off order placed for the BUY hedge leg.
    3. Critical audit event dispatched with exact description.
    4. Active order cleared (FSM safe idle).
    """
    dispatched_messages = []

    def mock_dispatch(msg: AgentMessage):
        dispatched_messages.append(msg)

    devops = DevOpsAgent(
        dispatch_fn=mock_dispatch,
        paper_trading=True,
    )
    # Enable simulated 2nd leg timeout
    devops.simulate_leg2_timeout = True

    spread_payload = {
        "trade_id": "SPD-SAFETY-001",
        "spread_type": "BULL_PUT_SPREAD",
        "net_credit": 27.5,
        "legs": [
            {
                "symbol": "NIFTY_24950_PE",
                "strike": 24950.0,
                "option_type": "PE",
                "action": "BUY",
                "quantity": 65,
                "price": 47.5,
            },
            {
                "symbol": "NIFTY_25000_PE",
                "strike": 25000.0,
                "option_type": "PE",
                "action": "SELL",
                "quantity": 65,
                "price": 75.0,
            },
        ],
    }

    # Dispatching order should trigger timeout guard and safe idle transition
    devops.dispatch_order(spread_payload)

    # 1. Active order must be None (safe idle)
    assert devops.active_order is None, "FSM state must transition to safe idle"

    # 2. Emergency square-off orders must have recorded the BUY leg unwinding
    assert len(devops.emergency_square_off_orders) == 1
    sq_order = devops.emergency_square_off_orders[0]
    assert sq_order["action"] == "SELL"  # Square off BUY position
    assert sq_order["quantity"] == 65
    assert sq_order["reason"] == "EMERGENCY_SQUARE_OFF"
    assert sq_order["ordertype"] == "LIMIT"
    assert sq_order["price"] == round(max(0.05, 47.5 - 0.50), 2)

    # 3. Critical audit event must be dispatched to Auditor
    assert len(dispatched_messages) == 1
    audit_msg = dispatched_messages[0]
    assert audit_msg.recipient == "Auditor"
    assert audit_msg.payload["event"] == "LEG_FILL_TIMEOUT"
    assert "LEG_FILL_TIMEOUT: Emergency square-off executed to prevent naked long hedge" in audit_msg.payload["reason"]


def test_execution_engine_2_leg_fill_timeout_and_square_off(tmp_path):
    """
    Verifies ExecutionEngine handles live broker second leg timeout:
    - BUY leg order placed and filled.
    - SELL leg order placed but times out after 5.0 seconds.
    - Cancels SELL order, places emergency MARKET square-off for BUY leg.
    - Sets active_spread to None and returns False.
    """
    mock_smart_api = MagicMock()
    # 1st call (BUY order): returns orderid "OID-BUY-101"
    # 2nd call (SELL order): returns orderid "OID-SELL-102"
    # 3rd call (Emergency square-off): returns orderid "OID-SQ-103"
    mock_smart_api.placeOrder.side_effect = [
        {"status": True, "data": {"orderid": "OID-BUY-101"}},
        {"status": True, "data": {"orderid": "OID-SELL-102"}},
        {"status": True, "data": {"orderid": "OID-SQ-103"}},
    ]
    # orderBook always returns "PENDING" to simulate timeout
    mock_smart_api.orderBook.return_value = {
        "status": True,
        "data": [
            {"orderid": "OID-SELL-102", "orderstatus": "OPEN"},
        ],
    }

    mock_auth = MagicMock()
    mock_auth.is_authenticated = True
    mock_auth.smart_api = mock_smart_api

    rg = RiskGuard(state_file=tmp_path / "temp_risk_state.json")
    engine = ExecutionEngine(risk_guard=rg, auth=mock_auth, paper_trading=False, lot_size=65)

    # Monkeypatch _wait_for_leg_fill timeout to 0.1s for fast unit testing
    orig_wait = engine._wait_for_leg_fill
    engine._wait_for_leg_fill = lambda oid, timeout_sec=5.0: orig_wait(oid, timeout_sec=0.1)

    spread = SpreadTrade(
        trade_id="SPD-TIMEOUT-TEST",
        spread_type=SpreadType.BULL_PUT_SPREAD,
        legs=[
            SpreadLeg(symbol="NIFTY_24950_PE", strike=24950.0, option_type="PE", action="BUY", quantity=65, price=47.5),
            SpreadLeg(symbol="NIFTY_25000_PE", strike=25000.0, option_type="PE", action="SELL", quantity=65, price=75.0),
        ],
        net_credit=27.5,
        max_risk_inr=1462.5,
        max_reward_inr=1787.5,
        timestamp=datetime(2026, 10, 5, 10, 15, tzinfo=IST),
        status="PROPOSED",
    )

    success = engine.execute_spread(spread)

    assert success is False
    assert engine.active_spread is None

    # Verify cancelOrder was called for the pending SELL order
    mock_smart_api.cancelOrder.assert_called_with("OID-SELL-102", "NORMAL")

    # Verify placeOrder was called 3 times (BUY, SELL, Emergency Square-off)
    assert mock_smart_api.placeOrder.call_count == 3
    sq_call = mock_smart_api.placeOrder.call_args_list[2][0][0]
    assert sq_call["transactiontype"] == "SELL"
    assert sq_call["ordertype"] == "LIMIT"
    assert sq_call["price"] == str(round(max(0.05, 47.5 - 0.50), 2))
    assert sq_call["quantity"] == "65"


def test_auditor_rejects_non_lot_size_65_orders(tmp_path):
    """
    Verifies AuditorAgent asserts order.quantity % 65 == 0,
    rejecting any non-multiple of 65 and approving multiples of 65.
    """
    dispatched = []

    def mock_dispatch(msg):
        dispatched.append(msg)

    auditor = AuditorAgent(
        dispatch_fn=mock_dispatch,
        state_file=str(tmp_path / "auditor_state.json"),
        db_path=str(tmp_path / "auditor_journal.db"),
    )

    # Order with quantity 25 (non-multiple of 65) -> REJECTED
    dispatched.clear()
    msg_bad = AgentMessage(
        msg_id="TEST-1",
        sender="Coder",
        recipient="Auditor",
        msg_type=MessageType.PROPOSED_ORDER,
        payload={
            "trade_id": "TRADE-BAD-QTY",
            "spread_type": "BULL_PUT_SPREAD",
            "spread_width": 50.0,
            "net_credit": 27.5,
            "max_risk_inr": 800.0,
            "legs": [
                {"action": "BUY", "quantity": 25, "price": 47.5},
                {"action": "SELL", "quantity": 25, "price": 75.0},
            ],
            "timestamp": datetime(2026, 10, 5, 10, 15, tzinfo=IST),
        },
    )
    auditor.handle_message(msg_bad)
    assert len(dispatched) == 1
    assert dispatched[0].msg_type == MessageType.AUDIT_REJECTED
    assert "must strictly be a positive multiple of 65" in dispatched[0].payload["reason"]

    # Order with quantity 65 (valid multiple) -> APPROVED
    dispatched.clear()
    msg_good = AgentMessage(
        msg_id="TEST-2",
        sender="Coder",
        recipient="Auditor",
        msg_type=MessageType.PROPOSED_ORDER,
        payload={
            "trade_id": "TRADE-GOOD-QTY",
            "spread_type": "BULL_PUT_SPREAD",
            "spread_width": 50.0,
            "net_credit": 27.5,
            "max_risk_inr": 1462.5,
            "legs": [
                {"action": "BUY", "quantity": 65, "price": 47.5},
                {"action": "SELL", "quantity": 65, "price": 75.0},
            ],
            "timestamp": datetime(2026, 10, 5, 10, 15, tzinfo=IST),
        },
    )
    auditor.handle_message(msg_good)
    assert len(dispatched) == 1
    assert dispatched[0].msg_type == MessageType.AUDIT_APPROVED
