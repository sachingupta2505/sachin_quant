"""
Comprehensive Unit Tests for Module 4: AuditLogger
Validates SQLite trade journal, MAE, MFE, slippage, and daily performance metrics.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import pytest

from audit_logger import AuditLogger, IST


@pytest.fixture
def temp_db(tmp_path: Path) -> Path:
    return tmp_path / "test_trading_journal.db"


def test_audit_logger_init(temp_db: Path):
    logger = AuditLogger(db_path=temp_db)
    assert temp_db.exists()
    perf = logger.get_daily_performance("2026-10-05")
    assert perf["trade_count"] == 0
    assert perf["total_pnl"] == 0.0


def test_log_entry_and_slippage(temp_db: Path):
    logger = AuditLogger(db_path=temp_db)
    entry_dt = datetime(2026, 10, 5, 10, 0, 0, tzinfo=IST)

    # Expected fill at 18.00, actual filled at 18.25 -> 0.25 positive slippage
    entry = logger.log_trade_entry(
        trade_id="TR-001",
        symbol="NIFTY",
        spread_type="BULL_PUT_SPREAD",
        expected_entry_price=18.00,
        actual_entry_price=18.25,
        is_paper=True,
        timestamp=entry_dt,
        notes="Test Bull Put Spread",
    )

    assert entry.trade_id == "TR-001"
    assert entry.entry_slippage == 0.25
    assert entry.status == "OPEN"

    trade = logger.get_trade("TR-001")
    assert trade is not None
    assert trade.actual_entry_price == 18.25
    assert trade.entry_slippage == 0.25


def test_mae_and_mfe_tracking(temp_db: Path):
    logger = AuditLogger(db_path=temp_db)
    entry_dt = datetime(2026, 10, 5, 10, 15, 0, tzinfo=IST)
    logger.log_trade_entry("TR-002", "NIFTY", "BEAR_CALL_SPREAD", 20.0, 20.0, timestamp=entry_dt)

    # Price moves favorably: +300 INR
    mae, mfe = logger.update_m2m("TR-002", 300.0)
    assert mae == 0.0
    assert mfe == 300.0

    # Price pulls back into adverse territory: -450 INR
    mae, mfe = logger.update_m2m("TR-002", -450.0)
    assert mae == -450.0
    assert mfe == 300.0

    # Price moves further into favor: +850 INR
    mae, mfe = logger.update_m2m("TR-002", 850.0)
    assert mae == -450.0
    assert mfe == 850.0

    # Exit trade at +700 INR (expected exit 5.0, actual 5.1 -> exit slippage 0.1)
    exit_dt = datetime(2026, 10, 5, 10, 45, 0, tzinfo=IST)
    closed = logger.log_trade_exit(
        trade_id="TR-002",
        expected_exit_price=5.0,
        actual_exit_price=5.1,
        realized_pnl=700.0,
        timestamp=exit_dt,
    )

    assert closed.status == "CLOSED"
    assert closed.realized_pnl == 700.0
    assert closed.mae_inr == -450.0
    assert closed.mfe_inr == 850.0
    assert closed.exit_slippage == 0.1
    assert closed.total_slippage == 0.1


def test_daily_performance_summary(temp_db: Path):
    logger = AuditLogger(db_path=temp_db)
    d1 = datetime(2026, 10, 5, 10, 0, 0, tzinfo=IST)

    # Trade 1: Win (+600 INR)
    logger.log_trade_entry("T1", "NIFTY", "BULL_PUT_SPREAD", 15.0, 15.0, timestamp=d1)
    logger.update_m2m("T1", -100.0)
    logger.update_m2m("T1", 700.0)
    logger.log_trade_exit("T1", 5.0, 5.0, realized_pnl=600.0, timestamp=d1)

    # Trade 2: Loss (-300 INR)
    logger.log_trade_entry("T2", "NIFTY", "BEAR_CALL_SPREAD", 20.0, 20.2, timestamp=d1)
    logger.update_m2m("T2", -400.0)
    logger.log_trade_exit("T2", 25.0, 25.0, realized_pnl=-300.0, timestamp=d1)

    perf = logger.get_daily_performance("2026-10-05")
    assert perf["trade_count"] == 2
    assert perf["total_pnl"] == 300.0  # 600 - 300
    assert perf["wins"] == 1
    assert perf["losses"] == 1
    assert perf["win_rate"] == 50.0
    assert perf["avg_mae"] == -250.0  # (-100 + -400) / 2
    assert perf["avg_mfe"] == 350.0   # (700 + 0) / 2


def test_invalid_trade_exit_raises_error(temp_db: Path):
    logger = AuditLogger(db_path=temp_db)
    with pytest.raises(ValueError):
        logger.log_trade_exit("NON_EXISTENT", 10.0, 10.0, 0.0)
