"""
Comprehensive Unit Tests for Module 3: ExecutionEngine
Validates Angel One TOTP auth, S/R zone identification, 5-min rejection detection (wick ratio >= 50%),
and defined-risk option spread execution (Paper Trading toggle).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import pytest

from execution_engine import (
    AngelAuth,
    ExecutionEngine,
    RejectionDetector,
    RejectionSignal,
    SignalType,
    SpreadType,
    SRZone,
)
from regime_filter import Candle, InitialBalance, IST
from risk_guard import RiskGuard, RiskState


@pytest.fixture
def temp_rg(tmp_path: Path) -> RiskGuard:
    return RiskGuard(state_file=tmp_path / "test_exec_state.json")


def make_candle(
    hour: int,
    minute: int,
    open_p: float,
    high_p: float,
    low_p: float,
    close_p: float,
    volume: float = 1000.0,
) -> Candle:
    return Candle(
        timestamp=datetime(2026, 10, 5, hour, minute, 0, tzinfo=IST),
        open=open_p,
        high=high_p,
        low=low_p,
        close=close_p,
        volume=volume,
    )


def test_angel_totp_generation():
    # Use standard RFC 6238 base32 test key
    test_secret = "JBSWY3DPEHPK3PXP"
    auth = AngelAuth(totp_secret=test_secret)
    totp = auth.generate_totp()
    assert isinstance(totp, str)
    assert len(totp) == 6
    assert totp.isdigit()


def test_angel_mock_auth_mode():
    auth = AngelAuth()  # No credentials
    assert auth.login() is False
    assert auth.is_authenticated is False


def test_sr_zones_calculation():
    detector = RejectionDetector()
    ib = InitialBalance(high=25100.0, low=24950.0, range=150.0, mid=25025.0, is_locked=True, candle_count=6)
    zones = detector.identify_sr_zones(ib, spot_price=25020.0)

    zone_names = [z.name for z in zones]
    assert "IB_HIGH_RESISTANCE" in zone_names
    assert "IB_LOW_SUPPORT" in zone_names
    # Strike boundaries (around 25000: 25050 and 24950)
    assert any("STRIKE_RES" in z.name for z in zones)
    assert any("STRIKE_SUP" in z.name for z in zones)


def test_bullish_rejection_candle_detection():
    detector = RejectionDetector(min_wick_ratio=0.50, min_candle_range=12.0)
    support_zone = SRZone(name="TEST_SUPPORT", level=25000.0, band_pts=10.0)

    # Bullish Hammer testing 25000 Support:
    # Open: 25030, High: 25035, Low: 24990 (tests 25000), Close: 25030
    # Range: 25035 - 24990 = 45 pts.
    # Lower Wick: 25030 - 24990 = 40 pts.
    # Lower Wick Ratio: 40 / 45 = 88.9% >= 50%
    candle = make_candle(10, 15, open_p=25030.0, high_p=25035.0, low_p=24990.0, close_p=25030.0)

    signal = detector.detect_rejection(candle, [support_zone])
    assert signal.signal_type == SignalType.BULLISH_REJECTION
    assert signal.wick_ratio >= 0.50
    assert signal.zone.name == "TEST_SUPPORT"
    assert "Bullish Rejection" in signal.description


def test_bearish_rejection_candle_detection():
    detector = RejectionDetector(min_wick_ratio=0.50, min_candle_range=12.0)
    resistance_zone = SRZone(name="TEST_RESISTANCE", level=25100.0, band_pts=10.0)

    # Bearish Shooting Star testing 25100 Resistance:
    # Open: 25070, High: 25110 (spikes into zone), Low: 25065, Close: 25070
    # Range: 25110 - 25065 = 45 pts.
    # Upper Wick: 25110 - 25070 = 40 pts.
    # Upper Wick Ratio: 40 / 45 = 88.9% >= 50%
    candle = make_candle(10, 30, open_p=25070.0, high_p=25110.0, low_p=25065.0, close_p=25070.0)

    signal = detector.detect_rejection(candle, [resistance_zone])
    assert signal.signal_type == SignalType.BEARISH_REJECTION
    assert signal.wick_ratio >= 0.50
    assert signal.zone.name == "TEST_RESISTANCE"
    assert "Bearish Rejection" in signal.description


def test_non_rejection_candle():
    detector = RejectionDetector()
    zone = SRZone(name="TEST_ZONE", level=25000.0)
    # Balanced body, tiny wicks
    candle = make_candle(10, 0, open_p=25000.0, high_p=25020.0, low_p=24995.0, close_p=25015.0)
    signal = detector.detect_rejection(candle, [zone])
    assert signal.signal_type == SignalType.NONE


def test_paper_trading_defined_risk_spread_execution(temp_rg: RiskGuard):
    engine = ExecutionEngine(risk_guard=temp_rg, paper_trading=True, lot_size=25, spread_width_pts=50.0)
    assert engine.paper_trading is True

    # Bullish Signal
    sup_zone = SRZone(name="IB_LOW_SUPPORT", level=25000.0)
    candle = make_candle(10, 15, open_p=25030.0, high_p=25035.0, low_p=24990.0, close_p=25030.0)
    signal = RejectionSignal(
        signal_type=SignalType.BULLISH_REJECTION,
        candle=candle,
        wick_ratio=0.85,
        zone=sup_zone,
        confidence=0.9,
        description="Bullish hammer at support",
    )

    # 1. Build Defined-Risk Spread (Bull Put Spread)
    spread = engine.build_spread(signal, spot_price=25020.0, timestamp=candle.timestamp)
    assert spread is not None
    assert spread.spread_type == SpreadType.BULL_PUT_SPREAD
    assert len(spread.legs) == 2

    short_leg = spread.legs[0]
    long_leg = spread.legs[1]
    assert short_leg.action == "SELL"
    assert short_leg.option_type == "PE"
    assert short_leg.strike == 25000.0

    assert long_leg.action == "BUY"
    assert long_leg.option_type == "PE"
    assert long_leg.strike == 24950.0  # 50 pts OTM hedge

    # Max risk must be strictly capped and defined <= 1500 INR
    assert spread.max_risk_inr > 0
    assert spread.max_risk_inr <= 1500.0
    assert spread.max_risk_inr == (50.0 - spread.net_credit) * 25

    # 2. Execute Paper Trade
    executed = engine.execute_spread(spread)
    assert executed is True
    assert engine.active_spread is not None
    assert spread.status == "FILLED"
    assert "PAPER-ORD" in short_leg.order_id

    # RiskGuard state should now be IN_TRADE and trade_count = 1
    assert temp_rg.current_state == RiskState.IN_TRADE
    assert temp_rg.trade_count == 1

    # 3. Close Spread with profit (+600 INR)
    closed = engine.close_active_spread(realized_pnl=600.0, exit_time=datetime(2026, 10, 5, 10, 45, tzinfo=IST))
    assert closed is True
    assert engine.active_spread is None
    assert temp_rg.realized_pnl == 600.0
    assert temp_rg.trade_count == 1


def test_spread_execution_blocked_outside_trading_window(temp_rg: RiskGuard):
    engine = ExecutionEngine(risk_guard=temp_rg, paper_trading=True, lot_size=25)
    early_candle = make_candle(9, 20, 25000, 25010, 24980, 25005)
    signal = RejectionSignal(
        signal_type=SignalType.BULLISH_REJECTION,
        candle=early_candle,
        wick_ratio=0.7,
        zone=SRZone("TEST", 25000),
        confidence=0.8,
        description="Early hammer",
    )
    spread = engine.build_spread(signal, spot_price=25000.0, timestamp=early_candle.timestamp)
    # Execution must be blocked by RiskGuard because it's before 09:45 AM IST
    executed = engine.execute_spread(spread)
    assert executed is False
    assert engine.active_spread is None


def test_spread_max_risk_strict_ceiling_enforced(temp_rg: RiskGuard):
    """
    Verifies that no option spread can ever be formed with Max Risk > 1500 INR.
    If spread width would breach 1500 INR, it must dynamically tighten or reject.
    """
    candle = make_candle(10, 15, open_p=25030.0, high_p=25035.0, low_p=24990.0, close_p=25030.0)
    signal = RejectionSignal(
        signal_type=SignalType.BULLISH_REJECTION,
        candle=candle,
        wick_ratio=0.85,
        zone=SRZone("TEST", 25000),
        confidence=0.9,
        description="Bullish test",
    )

    # Case 1: Standard 50-pt spread with lot_size=25 -> (50 - 18) * 25 = 800 INR <= 1500 INR (PASS)
    engine_safe = ExecutionEngine(risk_guard=temp_rg, paper_trading=True, lot_size=25, spread_width_pts=50.0)
    spread_safe = engine_safe.build_spread(signal, spot_price=25020.0, timestamp=candle.timestamp)
    assert spread_safe is not None
    assert spread_safe.max_risk_inr <= 1500.0

    # Case 2: Desired width is 100 pts, which at lot_size 25 would be > 1500 INR.
    # It must dynamically tighten to 50 pts so max risk <= 1500 INR.
    engine_wide = ExecutionEngine(risk_guard=temp_rg, paper_trading=True, lot_size=25, spread_width_pts=100.0)
    spread_tightened = engine_wide.build_spread(signal, spot_price=25020.0, timestamp=candle.timestamp)
    assert spread_tightened is not None
    assert spread_tightened.max_risk_inr <= 1500.0
    assert spread_tightened.metadata["width"] == 50.0  # Dynamically tightened to 50 pts

    # Case 3: Oversized lot size (lot_size=75), where even minimum 50 pt spread is (50 - 18) * 75 = 2400 INR > 1500 INR.
    # Must strictly REJECT the trade formulation and return None.
    engine_oversized = ExecutionEngine(risk_guard=temp_rg, paper_trading=True, lot_size=75, spread_width_pts=50.0)
    spread_rejected = engine_oversized.build_spread(signal, spot_price=25020.0, timestamp=candle.timestamp)
    assert spread_rejected is None


def test_coder_agent_spread_max_risk_1500_ceiling():
    """
    Verifies CoderAgent strictly enforces (Spread Width * Lot Size) - Net Premium <= 1500 INR.
    """
    from agents.coder import CoderAgent

    dispatched_messages = []

    def mock_dispatch(msg):
        dispatched_messages.append(msg)

    # Test 1: Safe lot size (25) -> Dispatches proposed order with max_risk_inr <= 1500 INR
    coder_safe = CoderAgent(dispatch_fn=mock_dispatch, spread_width=50.0, lot_size=25)
    coder_safe.on_strategy_signal({
        "signal_type": SignalType.BULLISH_REJECTION.value,
        "spot_price": 25020.0,
        "description": "Bullish hammer at support",
        "timestamp": datetime(2026, 10, 5, 10, 15, tzinfo=IST),
    })

    assert len(dispatched_messages) == 1
    proposed = dispatched_messages[0].payload
    assert proposed["max_risk_inr"] <= 1500.0
    assert (proposed["spread_width"] * 25) - (proposed["net_credit"] * 25) <= 1500.0

    # Test 2: Oversized lot size (75) where 50 pt spread yields 2400 INR > 1500 INR
    # CoderAgent MUST reject trade formulation and NOT dispatch any order
    dispatched_messages.clear()
    coder_oversized = CoderAgent(dispatch_fn=mock_dispatch, spread_width=50.0, lot_size=75)
    coder_oversized.on_strategy_signal({
        "signal_type": SignalType.BULLISH_REJECTION.value,
        "spot_price": 25020.0,
        "description": "Bullish hammer at support",
        "timestamp": datetime(2026, 10, 5, 10, 15, tzinfo=IST),
    })

    assert len(dispatched_messages) == 0  # Strictly blocked and aborted

