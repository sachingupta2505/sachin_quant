"""
Unit Tests for Autonomous Post-Market AI Chief Agent
Module: tests/test_post_market_ai_chief.py
"""

from __future__ import annotations

import json
from pathlib import Path
import pytest

from agents.post_market_ai_chief import (
    PostMarketAIChief,
    tool_read_file,
    tool_write_file,
    tool_run_terminal,
    tool_query_db,
    tool_fetch_market_levels,
    tool_arm_system_state,
)
from scripts.register_post_market_task import query_post_market_task

ROOT_DIR = Path(__file__).resolve().parent.parent


def test_tool_read_and_write(tmp_path: Path):
    test_file = tmp_path / "test_sample.txt"
    w_res = tool_write_file(str(test_file), "Hello AI Chief!")
    assert "[SUCCESS]" in w_res
    assert test_file.exists()

    content = tool_read_file(str(test_file))
    assert content == "Hello AI Chief!"


def test_tool_run_terminal():
    out = tool_run_terminal("python -c \"print('Hello from terminal tool')\"")
    assert "ExitCode: 0" in out
    assert "Hello from terminal tool" in out


def test_tool_query_db():
    out = tool_query_db("SELECT name FROM sqlite_master WHERE type='table'")
    assert "trade_journal" in out or "[" in out


def test_tool_fetch_market_levels():
    levels_str = tool_fetch_market_levels()
    levels = json.loads(levels_str)
    assert "pdh" in levels
    assert "pdl" in levels
    assert "pdc" in levels
    assert "pwh" in levels
    assert "pwl" in levels
    assert "atr_14" in levels
    assert levels["pdh"] > 0
    assert levels["atr_14"] > 0


def test_tool_arm_system_state():
    res = tool_arm_system_state()
    assert "[SUCCESS]" in res
    daily_state_path = ROOT_DIR / "daily_state.json"
    assert daily_state_path.exists()
    state = json.loads(daily_state_path.read_text(encoding="utf-8"))
    assert state["state"] == "ARMED_FOR_NEXT_SESSION"
    assert state["trade_count"] == 0
    assert state["realized_pnl"] == 0.0


def test_post_market_ai_chief_e2e_run():
    chief = PostMarketAIChief(target_date="2026-10-06")
    briefing = chief.run()

    assert briefing is not None
    assert len(briefing) > 100
    assert "Executive Summary & Session Performance" in briefing
    assert "Next-Session Reference Levels" in briefing

    # Verify artifacts
    levels_file = ROOT_DIR / "data" / "next_session_levels.json"
    assert levels_file.exists()
    levels = json.loads(levels_file.read_text(encoding="utf-8"))
    assert levels["status"] == "ARMED"

    briefing_file = ROOT_DIR / "reports" / "eod_briefing_2026-10-06.md"
    assert briefing_file.exists()

    # Verify Task Scheduler record
    task_info = query_post_market_task()
    assert task_info.get("Status") == "Ready"
