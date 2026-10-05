"""
Unit Tests for Strategy Logic Hardening:
1. OTM credit spread pricing with dynamic 2x credit stop-loss risk check (<= 1500 INR).
2. Two-candle confirmation filter for 5-min wick rejection at S/R zones.
3. Expiry day guardrails (Tuesday/Thursday weekly expiry):
   - Fresh entry freeze post-12:30 IST.
   - Mandatory square-off post-13:30 IST.
"""

from datetime import datetime, time
from zoneinfo import ZoneInfo
import pytest

from agents.base import AgentMessage, MessageType
from agents.architect import (
    ArchitectAgent,
    is_expiry_day,
    EXPIRY_ENTRY_CUTOFF_TIME,
    EXPIRY_SQUARE_OFF_TIME,
)
from agents.coder import CoderAgent, MAX_PERMITTED_SPREAD_RISK_INR
from agents.auditor import AuditorAgent
from execution_engine import (
    Candle,
    RejectionDetector,
    RejectionSignal,
    SignalType,
    SpreadType,
    SRZone,
)
from risk_guard import RiskGuard, RiskState

IST = ZoneInfo("Asia/Kolkata")


# =========================================================================
# 1. OTM Spread Pricing & 2x Credit Stop-Loss Risk Check
# =========================================================================
def test_otm_spread_pricing_and_stop_loss_risk_math():
    """
    Verifies CoderAgent synthesizes safe OTM credit spreads targeting 10-16 pts credit
    and enforces a defined stop-loss capped at min(credit * 2.0, 20 pts).
    With lot size 65, per-trade stop-loss risk is <= 1300 INR (safely <= 1500 INR limit).
    """
    dispatched = []

    def mock_dispatch(msg: AgentMessage):
        dispatched.append(msg)

    coder = CoderAgent(dispatch_fn=mock_dispatch, spread_width=50.0, lot_size=65)

    # Bullish signal at spot 25030 -> ATM 25000 -> OTM short put 24950, hedge 24900
    coder.on_strategy_signal({
        "signal_type": SignalType.BULLISH_REJECTION.value,
        "spot_price": 25030.0,
        "description": "Bullish rejection at support",
        "timestamp": datetime(2026, 10, 6, 10, 15, tzinfo=IST),
    })

    assert len(dispatched) == 1
    payload = dispatched[0].payload

    # OTM spread verification
    assert payload["spread_type"] == SpreadType.BULL_PUT_SPREAD.value
    assert payload["spread_width"] == 50.0
    assert 10.0 <= payload["net_credit"] <= 16.0

    # Stop loss definition
    expected_stop_pts = round(min(payload["net_credit"] * 2.0, 20.0), 2)
    assert payload["stop_loss_pts"] == expected_stop_pts
    assert payload["stop_loss_pts"] <= 20.0

    # Defined risk under 65 lot size
    expected_risk_inr = round(expected_stop_pts * 65, 2)
    assert payload["stop_loss_risk_inr"] == expected_risk_inr
    assert payload["stop_loss_risk_inr"] <= 1300.0
    assert payload["max_risk_inr"] <= 1500.0


def test_auditor_approves_defined_stop_loss_under_1500_and_rejects_over():
    """
    Verifies AuditorAgent approves spreads where stop-loss defined risk <= 1500 INR
    and vetoes any spread where stop-loss risk exceeds 1500 INR.
    """
    dispatched_approved = []
    dispatched_vetoed = []

    def mock_dispatch(msg: AgentMessage):
        if msg.msg_type == MessageType.AUDIT_APPROVED:
            dispatched_approved.append(msg)
        elif msg.msg_type == MessageType.AUDIT_REJECTED:
            dispatched_vetoed.append(msg)

    auditor = AuditorAgent(dispatch_fn=mock_dispatch)

    # Valid spread with 20 pt stop on 65 lot = 1300 INR <= 1500 INR
    valid_payload = {
        "trade_id": "SPD-TEST-SAFE",
        "spread_type": "BULL_PUT_SPREAD",
        "legs": [
            {"action": "BUY", "quantity": 65, "strike": 24900.0, "price": 12.0},
            {"action": "SELL", "quantity": 65, "strike": 24950.0, "price": 25.0},
        ],
        "net_credit": 13.0,
        "stop_loss_pts": 20.0,
        "stop_loss_risk_inr": 1300.0,
        "max_risk_inr": 1300.0,
        "timestamp": datetime(2026, 10, 5, 10, 30, tzinfo=IST),
    }

    auditor.on_proposed_order(valid_payload)
    assert len(dispatched_approved) == 1
    assert len(dispatched_vetoed) == 0

    # Excessive risk spread: 30 pt stop on 65 lot = 1950 INR > 1500 INR
    excessive_payload = {
        "trade_id": "SPD-TEST-EXCESS",
        "spread_type": "BULL_PUT_SPREAD",
        "legs": [
            {"action": "BUY", "quantity": 65, "strike": 24900.0, "price": 10.0},
            {"action": "SELL", "quantity": 65, "strike": 24950.0, "price": 30.0},
        ],
        "net_credit": 20.0,
        "stop_loss_pts": 30.0,
        "stop_loss_risk_inr": 1950.0,
        "max_risk_inr": 1950.0,
        "timestamp": datetime(2026, 10, 5, 11, 0, tzinfo=IST),
    }

    auditor.on_proposed_order(excessive_payload)
    assert len(dispatched_vetoed) == 1
    assert "exceeds limit of INR 1500.0" in dispatched_vetoed[0].payload["reason"]


# =========================================================================
# 2. Candle Confirmation Filter
# =========================================================================
def test_rejection_detector_confirmation_validation():
    """
    Directly tests validate_confirmation method on RejectionDetector.
    """
    detector = RejectionDetector()

    rejection_candle = Candle(
        timestamp=datetime(2026, 10, 6, 10, 0, tzinfo=IST),
        open=25000.0,
        high=25010.0,
        low=24950.0,  # 50 pt lower wick
        close=25005.0,
        volume=5000.0,
    )
    bull_signal = RejectionSignal(
        signal_type=SignalType.BULLISH_REJECTION,
        candle=rejection_candle,
        wick_ratio=0.833,
        zone=SRZone(name="TEST_SUP", level=24960.0),
        confidence=0.9,
        description="Bullish hammer at support",
    )

    # Case A: Confirmation succeeds (crosses above rejection high 25010)
    confirming_candle = Candle(
        timestamp=datetime(2026, 10, 6, 10, 5, tzinfo=IST),
        open=25006.0,
        high=25025.0,  # Crosses above 25010
        low=24990.0,   # Does not break low 24950
        close=25020.0,
        volume=6000.0,
    )
    confirmed, reason = detector.validate_confirmation(bull_signal, confirming_candle)
    assert confirmed is True
    assert "Crossed above rejection high" in reason

    # Case B: Invalidation (breaks below rejection wick low 24950)
    invalidating_candle = Candle(
        timestamp=datetime(2026, 10, 6, 10, 5, tzinfo=IST),
        open=25000.0,
        high=25015.0,
        low=24940.0,  # Breaks below 24950
        close=24945.0,
        volume=7000.0,
    )
    confirmed, reason = detector.validate_confirmation(bull_signal, invalidating_candle)
    assert confirmed is False
    assert "broke below rejection low" in reason

    # Case C: Failed confirmation (inside bar, does not cross high)
    inside_candle = Candle(
        timestamp=datetime(2026, 10, 6, 10, 5, tzinfo=IST),
        open=25002.0,
        high=25008.0,  # Fails to cross 25010
        low=24980.0,
        close=25004.0,
        volume=3000.0,
    )
    confirmed, reason = detector.validate_confirmation(bull_signal, inside_candle)
    assert confirmed is False
    assert "Did not cross above rejection high" in reason


def test_architect_two_candle_confirmation_workflow():
    """
    Verifies ArchitectAgent holds signal on candle 1 and only dispatches when candle 2 confirms.
    """
    dispatched = []

    def mock_dispatch(msg: AgentMessage):
        dispatched.append(msg)

    architect = ArchitectAgent(dispatch_fn=mock_dispatch, require_confirmation=True)
    # Inject reference level at 24950
    architect.set_reference_levels(daily_levels={"pdl": 24950.0})

    # Prime IB and baseline candles
    for m in range(15, 45, 5):
        c = Candle(
            timestamp=datetime(2026, 10, 5, 9, m, tzinfo=IST),
            open=25000.0, high=25020.0, low=24980.0, close=25000.0, volume=1000.0
        )
        architect.on_candle(c)

    # Candle 1 (10:00 IST): Forms hammer at 24950 support
    c1 = Candle(
        timestamp=datetime(2026, 10, 5, 10, 0, tzinfo=IST),
        open=24990.0,
        high=25005.0,
        low=24945.0,  # Touches 24950 support
        close=25000.0,
        volume=5000.0,
    )
    architect.on_candle(c1)

    # Must NOT have dispatched immediately; held in pending_rejection
    assert len(dispatched) == 0
    assert architect.pending_rejection is not None

    # Candle 2 (10:05 IST): Crosses above c1.high (25005)
    c2 = Candle(
        timestamp=datetime(2026, 10, 5, 10, 5, tzinfo=IST),
        open=25001.0,
        high=25020.0,  # Crosses 25005
        low=24990.0,
        close=25015.0,
        volume=6000.0,
    )
    architect.on_candle(c2)

    # Now signal must be dispatched with confirmation
    assert len(dispatched) == 1
    assert architect.pending_rejection is None
    payload = dispatched[0].payload
    assert payload["signal_type"] == SignalType.BULLISH_REJECTION.value
    assert "Confirmed" in payload["description"]


# =========================================================================
# 3. Expiry Day Guardrails (Tuesday / Thursday Weekly Expiry)
# =========================================================================
def test_expiry_day_identification():
    """
    Verifies is_expiry_day correctly recognizes Tuesday (1) and Thursday (3).
    """
    tuesday = datetime(2026, 10, 6, 10, 0, tzinfo=IST)   # Tuesday
    thursday = datetime(2026, 10, 8, 10, 0, tzinfo=IST)  # Thursday
    wednesday = datetime(2026, 10, 7, 10, 0, tzinfo=IST) # Wednesday
    monday = datetime(2026, 10, 5, 10, 0, tzinfo=IST)    # Monday

    assert is_expiry_day(tuesday) is True
    assert is_expiry_day(thursday) is True
    assert is_expiry_day(wednesday) is False
    assert is_expiry_day(monday) is False


def test_architect_blocks_entries_post_1230_on_expiry():
    """
    Verifies ArchitectAgent freezes strategy signals post-12:30 IST on expiry days.
    """
    dispatched = []

    def mock_dispatch(msg: AgentMessage):
        dispatched.append(msg)

    architect = ArchitectAgent(dispatch_fn=mock_dispatch, require_confirmation=False)
    architect.set_reference_levels(daily_levels={"pdl": 24950.0})

    # Prime IB
    for m in range(15, 45, 5):
        c = Candle(
            timestamp=datetime(2026, 10, 6, 9, m, tzinfo=IST),
            open=25000.0, high=25020.0, low=24980.0, close=25000.0, volume=1000.0
        )
        architect.on_candle(c)

    # Candle at 12:35 IST on Tuesday (Expiry day)
    c_late = Candle(
        timestamp=datetime(2026, 10, 6, 12, 35, tzinfo=IST),
        open=24990.0,
        high=25005.0,
        low=24945.0,
        close=25000.0,
        volume=5000.0,
    )
    architect.on_candle(c_late)

    assert len(dispatched) == 0, "Signals must be blocked post-12:30 on expiry days"


def test_auditor_blocks_entries_post_1230_and_mandates_square_off_post_1330():
    """
    Verifies AuditorAgent:
    1. Vetoes fresh orders after 12:30 IST on expiry days.
    2. Mandates emergency square-off after 13:30 IST on expiry days.
    """
    dispatched = []

    def mock_dispatch(msg: AgentMessage):
        dispatched.append(msg)

    auditor = AuditorAgent(dispatch_fn=mock_dispatch)

    # 1. Fresh order attempted at 12:35 IST on Tuesday
    order_payload = {
        "trade_id": "SPD-EXPIRY-1",
        "spread_type": "BULL_PUT_SPREAD",
        "legs": [
            {"action": "BUY", "quantity": 65, "strike": 24900.0, "price": 12.0},
            {"action": "SELL", "quantity": 65, "strike": 24950.0, "price": 25.0},
        ],
        "net_credit": 13.0,
        "stop_loss_pts": 20.0,
        "stop_loss_risk_inr": 1300.0,
        "max_risk_inr": 1300.0,
        "timestamp": datetime(2026, 10, 6, 12, 35, tzinfo=IST),
    }

    auditor.on_proposed_order(order_payload)
    assert len(dispatched) == 1
    assert dispatched[0].msg_type == MessageType.AUDIT_REJECTED
    assert "Expiry Day Guard" in dispatched[0].payload["reason"]

    # 2. Tick M2M at 13:35 IST triggers mandatory square-off
    dispatched.clear()
    auditor.audit_tick_m2m(
        trade_id="SPD-ACTIVE-POS",
        m2m_pnl=150.0,
        current_time=datetime(2026, 10, 6, 13, 35, tzinfo=IST),
    )
    assert len(dispatched) == 1
    assert dispatched[0].msg_type == MessageType.SQUARE_OFF_ALERT
    assert dispatched[0].payload["reason"] == "EXPIRY_MANDATORY_SQUARE_OFF_1330"


def test_risk_guard_expiry_fsm_transitions():
    """
    Verifies RiskGuard FSM blocks entry after 12:30 and enters SQUARE_OFF_TRIGGERED after 13:30 on expiry days.
    """
    rg = RiskGuard()

    # Normal day 12:45 IST -> permitted
    monday_noon = datetime(2026, 10, 5, 12, 45, tzinfo=IST)
    allowed, _ = rg.can_enter_trade(current_time=monday_noon)
    assert allowed is True

    # Tuesday (Expiry) 12:45 IST -> blocked
    tuesday_noon = datetime(2026, 10, 6, 12, 45, tzinfo=IST)
    allowed, reason = rg.can_enter_trade(current_time=tuesday_noon)
    assert allowed is False
    assert "Expiry day entry cutoff" in reason

    # Tuesday (Expiry) 13:35 IST -> SQUARE_OFF_TRIGGERED
    tuesday_afternoon = datetime(2026, 10, 6, 13, 35, tzinfo=IST)
    state = rg.evaluate_fsm(current_time=tuesday_afternoon)
    assert state == RiskState.SQUARE_OFF_TRIGGERED
