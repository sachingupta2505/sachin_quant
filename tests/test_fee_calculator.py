"""
Unit Tests for Indian Regulatory Fee Engine, Base Capital Tracking, and Audit Journal Integration
Tests cover:
1. Exact Rupee fee calculation for Nifty options spreads (Brokerage, STT, ETC, SEBI, Stamp Duty, GST).
2. GST base invariant: GST applies exclusively to (Brokerage + ETC + SEBI).
3. STT applies exclusively to sell-side turnover.
4. Stamp Duty applies exclusively to buy-side turnover.
5. Round-trip spread charges calculation (4 executed legs).
6. SQLite Trade Journal migration and persistence of gross_pnl, total_charges, and net_pnl.
7. Dashboard KPI metrics computation with starting capital and net ROI.
"""

import pytest
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
from fee_calculator import IndianRegulatoryFeeCalculator, FeeBreakdown
from audit_logger import AuditLogger, TradeAuditEntry
from dashboard import compute_kpis, load_trades_data
import pandas as pd


def test_regulatory_fee_rates_and_initialization():
    calc = IndianRegulatoryFeeCalculator()
    assert calc.starting_capital == 100000.0
    assert calc.brokerage_per_order == 20.0
    assert calc.stt_rate == 0.001
    assert calc.etc_rate == 0.0005
    assert calc.sebi_rate == 0.000001
    assert calc.stamp_duty_rate == 0.00003
    assert calc.gst_rate == 0.18


def test_exact_charges_sample_spread():
    calc = IndianRegulatoryFeeCalculator()
    # Sample spread: Buy 47.5, Sell 75.0, Quantity 65 (1 Nifty lot)
    buy_prem = 47.5
    sell_prem = 75.0
    qty = 65
    breakdown = calc.calculate_detailed_breakdown(buy_premium=buy_prem, sell_premium=sell_prem, quantity=qty, num_legs=2)

    # 1. Turnover
    expected_buy_turnover = 47.5 * 65  # 3087.5
    expected_sell_turnover = 75.0 * 65  # 4875.0
    expected_total_turnover = expected_buy_turnover + expected_sell_turnover  # 7962.5
    assert breakdown.buy_turnover == expected_buy_turnover
    assert breakdown.sell_turnover == expected_sell_turnover
    assert breakdown.total_turnover == expected_total_turnover

    # 2. Brokerage (2 legs * 20)
    assert breakdown.brokerage == 40.0

    # 3. STT (0.1% on sell turnover)
    assert breakdown.stt == round(4875.0 * 0.001, 2)  # 4.88

    # 4. ETC (0.05% on total turnover)
    assert breakdown.etc == round(7962.5 * 0.0005, 2)  # 3.98

    # 5. SEBI (₹10/crore on total turnover)
    assert breakdown.sebi == round(7962.5 * 0.000001, 2)  # 0.01

    # 6. Stamp duty (0.003% on buy turnover)
    assert breakdown.stamp_duty == round(3087.5 * 0.00003, 2)  # 0.09

    # 7. GST: 18% on (Brokerage + ETC + SEBI)
    # (40.0 + 3.98125 + 0.0079625) * 0.18 = 7.91805825 -> 7.92
    assert breakdown.gst == round((40.0 + 7962.5 * 0.0005 + 7962.5 * 0.000001) * 0.18, 2)

    # 8. Total charges (sum of rounded components: 40.0 + 4.88 + 3.98 + 0.01 + 0.09 + 7.92 = 56.88)
    total = calc.calculate_charges(buy_premium=buy_prem, sell_premium=sell_prem, quantity=qty, num_legs=2)
    assert total == 56.88
    assert breakdown.total_charges == 56.88


def test_gst_base_invariant():
    """Ensure GST does not tax STT or Stamp Duty (tax on tax prevention)."""
    calc = IndianRegulatoryFeeCalculator()
    breakdown = calc.calculate_detailed_breakdown(buy_premium=100.0, sell_premium=100.0, quantity=130, num_legs=2)
    taxable_services = breakdown.brokerage + breakdown.etc + breakdown.sebi
    expected_gst = round(taxable_services * 0.18, 2)
    assert breakdown.gst == expected_gst


def test_roundtrip_spread_charges():
    calc = IndianRegulatoryFeeCalculator()
    # 4 legs total: Entry Buy 40, Sell 80; Exit Sell 10, Buy 20, Qty 65
    breakdown = calc.calculate_spread_roundtrip_charges(
        buy_entry_premium=40.0,
        sell_entry_premium=80.0,
        buy_exit_premium=10.0,
        sell_exit_premium=20.0,
        quantity=65,
    )
    # 4 legs brokerage = 80 INR
    assert breakdown.brokerage == 80.0
    # Buy turnover: (40 + 20) * 65 = 3900.0
    assert breakdown.buy_turnover == 3900.0
    # Sell turnover: (80 + 10) * 65 = 5850.0
    assert breakdown.sell_turnover == 5850.0
    assert breakdown.total_charges > 80.0


def test_audit_logger_schema_and_net_pnl_persistence():
    with TemporaryDirectory() as tmp_dir:
        db_path = Path(tmp_dir) / "test_trading_journal.db"
        logger = AuditLogger(db_path=db_path)

        # Log entry
        logger.log_trade_entry(
            trade_id="TR-FEE-01",
            symbol="NIFTY",
            spread_type="BULL_PUT_SPREAD",
            expected_entry_price=35.0,
            actual_entry_price=35.0,
        )

        # Log exit with explicit gross_pnl and total_charges
        gross_pnl = 650.0
        total_charges = 56.87
        net_pnl = 593.13

        closed = logger.log_trade_exit(
            trade_id="TR-FEE-01",
            expected_exit_price=10.0,
            actual_exit_price=10.0,
            gross_pnl=gross_pnl,
            total_charges=total_charges,
            net_pnl=net_pnl,
        )

        assert closed.gross_pnl == 650.0
        assert closed.total_charges == 56.87
        assert closed.net_pnl == 593.13
        assert closed.realized_pnl == 593.13

        # Retrieve and verify persistence
        retrieved = logger.get_trade("TR-FEE-01")
        assert retrieved is not None
        assert retrieved.gross_pnl == 650.0
        assert retrieved.total_charges == 56.87
        assert retrieved.net_pnl == 593.13

        # Verify daily performance aggregates net PnL
        perf = logger.get_daily_performance(retrieved.date)
        assert perf["total_pnl"] == 593.13
        assert perf["total_net_pnl"] == 593.13
        assert perf["total_gross_pnl"] == 650.0
        assert perf["total_charges"] == 56.87


def test_dashboard_kpis_with_charges_and_capital():
    df = pd.DataFrame([
        {
            "trade_id": "T1",
            "status": "CLOSED",
            "realized_pnl": 593.13,
            "gross_pnl": 650.0,
            "total_charges": 56.87,
            "net_pnl": 593.13,
            "is_paper": 1,
        },
        {
            "trade_id": "T2",
            "status": "CLOSED",
            "realized_pnl": -256.87,
            "gross_pnl": -200.0,
            "total_charges": 56.87,
            "net_pnl": -256.87,
            "is_paper": 1,
        },
    ])

    kpis = compute_kpis(df, starting_capital=100000.0)
    assert kpis["starting_capital"] == 100000.0
    assert kpis["gross_pnl"] == 450.0
    assert round(kpis["total_charges"], 2) == 113.74
    assert round(kpis["net_pnl"], 2) == 336.26
    assert round(kpis["current_balance"], 2) == 100336.26
    assert round(kpis["net_roi_pct"], 2) == 0.34
    assert kpis["win_rate"] == 50.0
