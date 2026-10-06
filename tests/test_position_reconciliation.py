"""
Unit Tests for Position Reconciliation & EOD Square-Off
Module: tests/test_position_reconciliation.py
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
import pytest

from audit_logger import AuditLogger, IST
from fee_calculator import IndianRegulatoryFeeCalculator


@pytest.fixture
def temp_journal(tmp_path: Path) -> Path:
    db_file = tmp_path / "isolated_test_journal.db"
    yield db_file
    if db_file.exists():
        try:
            db_file.unlink()
        except Exception:
            pass


def test_reconcile_and_close_open_positions(temp_journal: Path):
    logger = AuditLogger(db_path=temp_journal, tz=IST)
    entry_dt = datetime(2026, 10, 6, 10, 15, 0, tzinfo=IST)

    # 1. Log an open trade
    logger.log_trade_entry(
        trade_id="TEST-REC-001",
        symbol="NIFTY",
        spread_type="BULL_PUT_SPREAD",
        expected_entry_price=13.0,
        actual_entry_price=13.0,
        is_paper=True,
        timestamp=entry_dt,
        notes="Bullish Hammer Support",
    )

    trade = logger.get_trade("TEST-REC-001")
    assert trade.status == "OPEN"

    # 2. Before reconciliation: get_daily_performance counts total trades including open
    perf_before = logger.get_daily_performance("2026-10-06")
    assert perf_before["trade_count"] == 1
    assert perf_before["total_trades"] == 1
    assert perf_before["closed_trades"] == 0
    assert perf_before["open_trades"] == 1

    # 3. Reconcile and close open positions
    exit_dt = datetime(2026, 10, 6, 15, 10, 0, tzinfo=IST)
    closed_list = logger.reconcile_and_close_open_positions(
        exit_time=exit_dt,
        notes="15:10 IST Auto Square-Off",
        lot_size=65,
    )

    assert len(closed_list) == 1
    closed_trade = closed_list[0]
    assert closed_trade.status == "CLOSED"
    assert closed_trade.actual_exit_price == 0.0
    assert closed_trade.gross_pnl == 845.0  # 13.0 * 65
    assert closed_trade.total_charges > 0.0
    assert closed_trade.net_pnl == round(845.0 - closed_trade.total_charges, 2)

    # 4. After reconciliation: get_daily_performance reflects 1 closed trade and 100% win rate
    perf_after = logger.get_daily_performance("2026-10-06")
    assert perf_after["trade_count"] == 1
    assert perf_after["total_trades"] == 1
    assert perf_after["closed_trades"] == 1
    assert perf_after["open_trades"] == 0
    assert perf_after["wins"] == 1
    assert perf_after["losses"] == 0
    assert perf_after["win_rate"] == 100.0
    assert perf_after["total_gross_pnl"] == 845.0
    assert perf_after["total_charges"] == closed_trade.total_charges
    assert perf_after["total_net_pnl"] == closed_trade.net_pnl


def test_reconcile_eod_with_broker_pnl(temp_journal: Path):
    logger = AuditLogger(db_path=temp_journal, tz=IST)
    entry_dt = datetime(2026, 10, 6, 10, 15, 0, tzinfo=IST)

    logger.log_trade_entry(
        trade_id="TEST-REC-002",
        symbol="NIFTY",
        spread_type="BULL_PUT_SPREAD",
        expected_entry_price=10.0,
        actual_entry_price=10.0,
        timestamp=entry_dt,
    )

    # Reconcile against expected broker PnL
    rec = logger.reconcile_eod(date_str="2026-10-06", broker_pnl=600.0, lot_size=65)
    assert rec["closed_trades"] == 1
    assert rec["open_trades"] == 0
    assert "discrepancy" in rec
    assert rec["broker_pnl"] == 600.0
    assert rec["newly_closed_count"] == 1
