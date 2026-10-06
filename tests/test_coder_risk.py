"""
Unit Tests for Coder Agent Strict Risk Sizing & Dynamic Spread Width Reduction
Invariant: Max Risk = ((Spread Width in Points * Lot Size) - Net Premium Received) <= 1500 INR
"""

from datetime import datetime
from zoneinfo import ZoneInfo
import pytest

from agents.coder import CoderAgent, MAX_PERMITTED_SPREAD_RISK_INR
from execution_engine import SignalType, SpreadType

IST = ZoneInfo("Asia/Kolkata")


def test_spread_dynamic_width_reduction_under_1500_inr():
    """
    When a wider spread width is requested (e.g. 150 pt or 100 pt), CoderAgent must
    dynamically step down candidate widths to 50 pt so Max Risk strictly respects <= 1500 INR.
    """
    dispatched = []

    def mock_dispatch(msg):
        dispatched.append(msg)

    # Initialize with 150 pt spread width and lot size 65
    coder = CoderAgent(dispatch_fn=mock_dispatch, spread_width=150.0, lot_size=65)

    coder.on_strategy_signal({
        "signal_type": SignalType.BULLISH_REJECTION.value,
        "spot_price": 25035.0,
        "description": "Bullish hammer near 25000 support",
        "timestamp": datetime(2026, 10, 5, 10, 30, tzinfo=IST),
    })

    assert len(dispatched) == 1, "Expected order proposal to be dispatched after width reduction"
    payload = dispatched[0].payload

    # Spread width must have been dynamically reduced from 150 to 50
    assert payload["spread_width"] == 50.0
    assert payload["max_risk_inr"] <= 1500.0
    assert payload["stop_loss_risk_inr"] <= 1500.0
    assert payload["stop_loss_pts"] <= 20.0
    assert payload["net_credit"] >= 10.0


def test_spread_rejection_when_cannot_meet_1500_inr():
    """
    If lot size is not a multiple of 65 or is too large such that even the minimum 50 pt spread
    breaches 1500 INR, CoderAgent must reject synthesis and not dispatch any proposal.
    """
    dispatched = []

    def mock_dispatch(msg):
        dispatched.append(msg)

    coder = CoderAgent(dispatch_fn=mock_dispatch, spread_width=50.0, lot_size=75)

    coder.on_strategy_signal({
        "signal_type": SignalType.BULLISH_REJECTION.value,
        "spot_price": 25035.0,
        "description": "Bullish hammer",
        "timestamp": datetime(2026, 10, 5, 10, 30, tzinfo=IST),
    })

    assert len(dispatched) == 0, "Trade synthesis must be rejected when risk exceeds 1500 INR"


def test_bear_call_spread_respects_1500_inr_max_risk():
    """
    Verifies Bear Call Spreads also enforce strict risk sizing <= 1500 INR.
    """
    dispatched = []

    def mock_dispatch(msg):
        dispatched.append(msg)

    coder = CoderAgent(dispatch_fn=mock_dispatch, spread_width=100.0, lot_size=65)

    coder.on_strategy_signal({
        "signal_type": SignalType.BEARISH_REJECTION.value,
        "spot_price": 25180.0,
        "description": "Shooting star at resistance",
        "timestamp": datetime(2026, 10, 5, 11, 45, tzinfo=IST),
    })

    assert len(dispatched) == 1
    payload = dispatched[0].payload

    assert payload["spread_type"] == SpreadType.BEAR_CALL_SPREAD.value
    assert payload["spread_width"] <= 50.0
    assert payload["max_risk_inr"] <= 1500.0
    assert payload["stop_loss_risk_inr"] <= 1500.0
    assert payload["stop_loss_pts"] <= 20.0


def test_comprehensive_spot_and_strike_sweep_never_exceeds_1500_inr():
    """
    Sweeps spot prices across typical Nifty ranges to confirm no synthesized spread
    can ever produce Max Risk > 1500 INR.
    """
    dispatched = []

    def mock_dispatch(msg):
        dispatched.append(msg)

    coder = CoderAgent(dispatch_fn=mock_dispatch, spread_width=50.0, lot_size=65)

    for spot in [24500.0, 24725.0, 25000.0, 25250.0, 25680.0]:
        for sig in [SignalType.BULLISH_REJECTION.value, SignalType.BEARISH_REJECTION.value]:
            dispatched.clear()
            coder.on_strategy_signal({
                "signal_type": sig,
                "spot_price": spot,
                "description": f"Test signal at {spot}",
                "timestamp": datetime(2026, 10, 5, 10, 0, tzinfo=IST),
            })
            assert len(dispatched) == 1
            payload = dispatched[0].payload
            assert payload["max_risk_inr"] <= 1500.0, f"Breached for spot {spot}"


def test_auditor_spread_width_minimum_credit_27_pts_invariant(tmp_path):
    """
    If physical max loss (spread_width - credit) * lot_size > 1500 INR,
    AuditorAgent must reject the trade if credit < 27.0 pts, and approve when credit >= 27.0 pts.
    """
    from agents.auditor import AuditorAgent
    from agents.base import AgentMessage, MessageType

    dispatched = []

    def mock_dispatch(msg):
        dispatched.append(msg)

    auditor = AuditorAgent(
        dispatch_fn=mock_dispatch,
        state_file=str(tmp_path / "aud_state.json"),
        db_path=str(tmp_path / "aud_db.db"),
    )

    # 1. 50-pt spread with low credit 15.0 pts: (50 - 15) * 65 = 2275 INR > 1500 INR -> REJECTED
    msg_low_credit = AgentMessage(
        msg_id="AUD-PROP-001",
        sender="Coder",
        recipient="Auditor",
        msg_type=MessageType.PROPOSED_ORDER,
        payload={
            "trade_id": "TRADE-PHYSICAL-RISK-HIGH",
            "spread_type": "BULL_PUT_SPREAD",
            "spread_width": 50.0,
            "net_credit": 15.0,
            "stop_loss_pts": 15.0,
            "stop_loss_risk_inr": 975.0,
            "max_risk_inr": 975.0,
            "legs": [
                {"action": "BUY", "quantity": 65, "price": 10.0},
                {"action": "SELL", "quantity": 65, "price": 25.0},
            ],
            "timestamp": datetime(2026, 10, 5, 10, 15, tzinfo=IST),
        },
    )
    auditor.handle_message(msg_low_credit)
    assert len(dispatched) == 1
    assert dispatched[0].msg_type == MessageType.AUDIT_REJECTED
    assert "Minimum required credit is >= 27.0 pts" in dispatched[0].payload["reason"]

    # 2. 50-pt spread with compliant credit >= 27.0 pts: (50 - 27) * 65 = 1495 INR <= 1500 INR -> APPROVED
    dispatched.clear()
    msg_compliant_credit = AgentMessage(
        msg_id="AUD-PROP-002",
        sender="Coder",
        recipient="Auditor",
        msg_type=MessageType.PROPOSED_ORDER,
        payload={
            "trade_id": "TRADE-PHYSICAL-RISK-COMPLIANT",
            "spread_type": "BULL_PUT_SPREAD",
            "spread_width": 50.0,
            "net_credit": 27.0,
            "stop_loss_pts": 20.0,
            "stop_loss_risk_inr": 1300.0,
            "max_risk_inr": 1300.0,
            "legs": [
                {"action": "BUY", "quantity": 65, "price": 11.0},
                {"action": "SELL", "quantity": 65, "price": 38.0},
            ],
            "timestamp": datetime(2026, 10, 5, 10, 20, tzinfo=IST),
        },
    )
    auditor.handle_message(msg_compliant_credit)
    assert len(dispatched) == 1
    assert dispatched[0].msg_type == MessageType.AUDIT_APPROVED
