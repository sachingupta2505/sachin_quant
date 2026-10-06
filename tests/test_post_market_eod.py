"""
Unit Tests for Static Post-Market Reconciler & Level Calculator
Module: tests/test_post_market_eod.py
"""

from __future__ import annotations

import json
from pathlib import Path
import pytest

from scripts.calculate_next_levels import calculate_levels
from scripts.post_market_eod import PostMarketReconciler

ROOT_DIR = Path(__file__).resolve().parent.parent


def test_calculate_next_levels():
    levels = calculate_levels("2026-10-06")
    assert "pdh" in levels
    assert "pdl" in levels
    assert "pdc" in levels
    assert "atr_14" in levels
    # Verify Nifty levels are in genuine ~22.5k - ~22.8k range, NOT 25k mock
    assert 22000.0 <= levels["pdh"] <= 23500.0
    assert 22000.0 <= levels["pdl"] <= 23500.0
    assert 22000.0 <= levels["pdc"] <= 23500.0
    assert levels["atr_14"] > 100.0
    assert levels["status"] == "ARMED"


def test_post_market_reconciler_zero_trades(tmp_path: Path):
    reconciler = PostMarketReconciler(target_date="2026-10-06")
    # Execute run
    summary = reconciler.run()
    assert summary["trades"] == 0
    assert summary["net_pnl"] == 0.0
    assert summary["state"] == "ARMED_FOR_NEXT_SESSION"

    # Verify daily_state.json
    state_file = ROOT_DIR / "daily_state.json"
    assert state_file.exists()
    state = json.loads(state_file.read_text(encoding="utf-8"))
    assert state["state"] == "ARMED_FOR_NEXT_SESSION"
    assert state["trade_count"] == 0
    assert state["realized_pnl"] == 0.0

    # Verify briefing report
    report_file = ROOT_DIR / "reports" / "eod_briefing_2026-10-06.md"
    assert report_file.exists()
    content = report_file.read_text(encoding="utf-8")
    assert "**Total Trades Executed:** 0" in content
    assert "**INR +0.00**" in content
