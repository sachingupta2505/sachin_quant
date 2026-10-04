"""
Comprehensive Unit Tests for Module 1: RiskGuard
Validates FSM transitions, daily loss kill switch (-1500 INR), max 2 trades/day,
trading window gating (09:45 - 15:05, 15:10 square-off), and atomic persistence.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import pytest

from risk_guard import (
    IST,
    MAX_DAILY_LOSS_INR,
    MAX_DAILY_TRADES,
    DailyRiskState,
    RiskGuard,
    RiskState,
)


@pytest.fixture
def temp_state_file(tmp_path: Path) -> Path:
    return tmp_path / "test_daily_state.json"


@pytest.fixture
def base_date() -> datetime:
    # A standard trading day at 10:00 AM IST (within 09:45 - 15:05 window)
    return datetime(2026, 10, 5, 10, 0, 0, tzinfo=IST)


def test_initial_state_within_trading_window(temp_state_file: Path, base_date: datetime):
    rg = RiskGuard(state_file=temp_state_file)
    state = rg.evaluate_fsm(base_date)
    assert state == RiskState.READY
    assert rg.trade_count == 0
    assert rg.realized_pnl == 0.0
    assert rg.unrealized_pnl == 0.0
    allowed, msg = rg.can_enter_trade(base_date)
    assert allowed is True
    assert "satisfied" in msg


def test_time_window_gating_before_open(temp_state_file: Path):
    rg = RiskGuard(state_file=temp_state_file)
    pre_open_time = datetime(2026, 10, 5, 9, 30, 0, tzinfo=IST)
    state = rg.evaluate_fsm(pre_open_time)
    assert state == RiskState.IDLE
    allowed, msg = rg.can_enter_trade(pre_open_time)
    assert allowed is False
    assert "Before trading window" in msg


def test_time_window_gating_at_open_and_close(temp_state_file: Path):
    rg = RiskGuard(state_file=temp_state_file)
    # Exactly at 09:45:00
    open_time = datetime(2026, 10, 5, 9, 45, 0, tzinfo=IST)
    assert rg.evaluate_fsm(open_time) == RiskState.READY
    assert rg.can_enter_trade(open_time)[0] is True

    # At 15:05:00 (Entry window cutoff)
    entry_cutoff_time = datetime(2026, 10, 5, 15, 5, 0, tzinfo=IST)
    assert rg.can_enter_trade(entry_cutoff_time)[0] is False


def test_square_off_trigger_at_15_10(temp_state_file: Path):
    rg = RiskGuard(state_file=temp_state_file)
    sq_off_time = datetime(2026, 10, 5, 15, 10, 0, tzinfo=IST)
    state = rg.evaluate_fsm(sq_off_time)
    assert state == RiskState.SQUARE_OFF_TRIGGERED
    assert rg.is_square_off_time(sq_off_time) is True
    assert rg.can_enter_trade(sq_off_time)[0] is False


def test_day_closed_after_market(temp_state_file: Path):
    rg = RiskGuard(state_file=temp_state_file)
    market_close = datetime(2026, 10, 5, 15, 30, 0, tzinfo=IST)
    assert rg.evaluate_fsm(market_close) == RiskState.DAY_CLOSED


def test_max_two_trades_per_day_limit(temp_state_file: Path, base_date: datetime):
    rg = RiskGuard(state_file=temp_state_file)

    # Trade 1
    t1_time = datetime(2026, 10, 5, 10, 15, tzinfo=IST)
    rg.record_trade_entry("TRADE-001", {"symbol": "NIFTY-OPT"}, current_time=t1_time)
    assert rg.current_state == RiskState.IN_TRADE
    assert rg.trade_count == 1
    assert rg.can_enter_trade(t1_time)[0] is False  # Cannot enter while in trade

    # Exit Trade 1 with +500 INR
    rg.record_trade_exit("TRADE-001", realized_pnl=500.0, current_time=datetime(2026, 10, 5, 10, 30, tzinfo=IST))
    assert rg.trade_count == 1
    assert rg.realized_pnl == 500.0
    assert rg.evaluate_fsm(datetime(2026, 10, 5, 10, 35, tzinfo=IST)) == RiskState.READY

    # Trade 2
    t2_time = datetime(2026, 10, 5, 11, 0, tzinfo=IST)
    rg.record_trade_entry("TRADE-002", {"symbol": "NIFTY-OPT"}, current_time=t2_time)
    assert rg.trade_count == 2
    rg.record_trade_exit("TRADE-002", realized_pnl=-300.0, current_time=datetime(2026, 10, 5, 11, 45, tzinfo=IST))
    assert rg.trade_count == 2
    assert rg.realized_pnl == 200.0

    # Trade 3 attempt: MUST be blocked
    t3_time = datetime(2026, 10, 5, 12, 0, tzinfo=IST)
    state = rg.evaluate_fsm(t3_time)
    assert state == RiskState.MAX_TRADES_REACHED
    allowed, msg = rg.can_enter_trade(t3_time)
    assert allowed is False
    assert "Maximum daily trade limit" in msg

    with pytest.raises(PermissionError):
        rg.record_trade_entry("TRADE-003", current_time=t3_time)


def test_kill_switch_realized_loss(temp_state_file: Path, base_date: datetime):
    rg = RiskGuard(state_file=temp_state_file)

    # Trade 1 has a severe loss of -1600 INR (exceeding -1500 INR limit)
    t1_time = datetime(2026, 10, 5, 10, 0, tzinfo=IST)
    rg.record_trade_entry("TRADE-001", current_time=t1_time)
    rg.record_trade_exit("TRADE-001", realized_pnl=-1600.0, current_time=datetime(2026, 10, 5, 10, 20, tzinfo=IST))

    assert rg.current_state == RiskState.KILL_SWITCH_ACTIVE
    assert rg.is_square_off_time(base_date) is True
    allowed, msg = rg.can_enter_trade(base_date)
    assert allowed is False
    assert "Kill-switch active" in msg


def test_kill_switch_unrealized_m2m_loss(temp_state_file: Path, base_date: datetime):
    rg = RiskGuard(state_file=temp_state_file)

    rg.record_trade_entry("TRADE-001", current_time=base_date)
    assert rg.current_state == RiskState.IN_TRADE

    # Live tick updates M2M to -1550 INR
    state = rg.update_unrealized_pnl(-1550.0, current_time=base_date)
    assert state == RiskState.KILL_SWITCH_ACTIVE
    assert rg.is_square_off_time(base_date) is True

    allowed, _ = rg.can_enter_trade(base_date)
    assert allowed is False


def test_state_persistence_crash_recovery(temp_state_file: Path, base_date: datetime):
    # Simulate first process run
    rg1 = RiskGuard(state_file=temp_state_file)
    t_time = datetime(2026, 10, 5, 10, 5, tzinfo=IST)
    rg1.record_trade_entry("TRADE-CRASH-TEST", {"strategy": "spread"}, current_time=t_time)
    rg1.record_trade_exit("TRADE-CRASH-TEST", realized_pnl=-450.0, current_time=datetime(2026, 10, 5, 10, 25, tzinfo=IST))

    assert temp_state_file.exists()

    # Simulate abrupt process death and restart: create brand new RiskGuard instance
    rg2 = RiskGuard(state_file=temp_state_file)
    assert rg2.trade_count == 1
    assert rg2.realized_pnl == -450.0
    assert len(rg2._data.trades) == 1
    assert rg2._data.trades[0]["trade_id"] == "TRADE-CRASH-TEST"
    assert rg2._data.trades[0]["status"] == "CLOSED"

    # Verify next trade count correctly increments from restored state
    next_time = datetime(2026, 10, 5, 11, 0, tzinfo=IST)
    assert rg2.can_enter_trade(next_time)[0] is True
    rg2.record_trade_entry("TRADE-002", current_time=next_time)
    assert rg2.trade_count == 2


def test_date_rollover_clears_previous_day_state(temp_state_file: Path):
    rg = RiskGuard(state_file=temp_state_file)
    day1_time = datetime(2026, 10, 5, 10, 0, tzinfo=IST)
    rg.record_trade_entry("DAY1-T1", current_time=day1_time)
    rg.record_trade_exit("DAY1-T1", realized_pnl=500.0, current_time=day1_time)
    assert rg.trade_count == 1

    # Next calendar day arrives
    day2_time = datetime(2026, 10, 6, 10, 0, tzinfo=IST)
    new_state = rg.evaluate_fsm(day2_time)
    assert new_state == RiskState.READY
    assert rg.trade_count == 0
    assert rg.realized_pnl == 0.0
    assert rg.can_enter_trade(day2_time)[0] is True


def test_corrupted_state_file_graceful_recovery(temp_state_file: Path):
    temp_state_file.write_text("{invalid_json: true, broken", encoding="utf-8")
    rg = RiskGuard(state_file=temp_state_file)
    assert rg.trade_count == 0
    assert rg.realized_pnl == 0.0
