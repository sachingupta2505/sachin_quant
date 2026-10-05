"""
Unit Tests for SachinQuant Streamlit Analytics Dashboard
Validates:
1. Graceful empty database handling.
2. KPI computation and data enrichment on populated mock database.
3. Read-only WAL connection pragma (PRAGMA query_only = ON).
4. Clean UI rendering simulation with Streamlit mock.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from dashboard import (
    compute_kpis,
    get_read_only_connection,
    load_trades_data,
    render_ui,
)


def create_mock_journal_db(db_path: Path, populate_trades: bool = True) -> Path:
    """Helper to initialize a test SQLite trading journal."""
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS trade_journal (
            trade_id TEXT PRIMARY KEY,
            date TEXT NOT NULL,
            symbol TEXT NOT NULL,
            spread_type TEXT NOT NULL,
            entry_time TEXT NOT NULL,
            expected_entry_price REAL NOT NULL,
            actual_entry_price REAL NOT NULL,
            entry_slippage REAL NOT NULL,
            exit_time TEXT,
            expected_exit_price REAL,
            actual_exit_price REAL,
            exit_slippage REAL DEFAULT 0.0,
            total_slippage REAL DEFAULT 0.0,
            realized_pnl REAL DEFAULT 0.0,
            mae_inr REAL DEFAULT 0.0,
            mfe_inr REAL DEFAULT 0.0,
            is_paper INTEGER NOT NULL,
            status TEXT NOT NULL,
            notes TEXT
        );
        """
    )
    if populate_trades:
        conn.execute(
            """
            INSERT INTO trade_journal VALUES (
                'SPD-TEST-001', '2026-10-05', 'NIFTY', 'BULL_PUT_SPREAD',
                '2026-10-05T10:15:00+05:30', 18.0, 18.0, 0.0,
                '2026-10-05T10:45:00+05:30', 5.0, 5.0, 0.0, 0.0,
                650.0, -100.0, 650.0, 1, 'CLOSED', 'Bullish Rejection at Support (25000.0)'
            );
            """
        )
        conn.execute(
            """
            INSERT INTO trade_journal VALUES (
                'SPD-TEST-002', '2026-10-05', 'NIFTY', 'BEAR_CALL_SPREAD',
                '2026-10-05T11:30:00+05:30', 16.0, 16.0, 0.0,
                '2026-10-05T12:00:00+05:30', 20.0, 20.0, 0.0, 0.0,
                -200.0, -200.0, 50.0, 1, 'CLOSED', 'Bearish Rejection at Resistance (25200.0)'
            );
            """
        )
        conn.execute(
            """
            INSERT INTO trade_journal VALUES (
                'SPD-TEST-003', '2026-10-05', 'NIFTY', 'BULL_PUT_SPREAD',
                '2026-10-05T13:00:00+05:30', 17.0, 17.0, 0.0,
                NULL, NULL, NULL, 0.0, 0.0,
                0.0, 0.0, 0.0, 1, 'OPEN', 'Active Position'
            );
            """
        )
    conn.commit()
    conn.close()
    return db_path


def test_empty_database_handling(tmp_path: Path):
    empty_db = tmp_path / "empty_journal.db"
    create_mock_journal_db(empty_db, populate_trades=False)

    df = load_trades_data(journal_db_path=empty_db)
    assert df.empty

    kpis = compute_kpis(df)
    assert kpis["net_pnl"] == 0.0
    assert kpis["win_rate"] == 0.0
    assert kpis["total_trades"] == 0
    assert kpis["closed_trades"] == 0
    assert kpis["max_risk_cap"] == 1500.0
    assert kpis["is_paper"] is True


def test_nonexistent_database_handling(tmp_path: Path):
    absent_db = tmp_path / "non_existent.db"

    df = load_trades_data(journal_db_path=absent_db)
    assert df.empty

    kpis = compute_kpis(df)
    assert kpis["net_pnl"] == 0.0
    assert kpis["total_trades"] == 0


def test_kpis_and_trades_enrichment_mock_data(tmp_path: Path):
    mock_db = tmp_path / "mock_journal.db"
    create_mock_journal_db(mock_db, populate_trades=True)

    df = load_trades_data(journal_db_path=mock_db)
    assert len(df) == 3
    assert "time_display" in df.columns
    assert "strategy_display" in df.columns
    assert "strikes" in df.columns

    kpis = compute_kpis(df)
    assert kpis["net_pnl"] == 450.0  # 650 - 200
    assert kpis["total_trades"] == 3
    assert kpis["closed_trades"] == 2
    assert kpis["winning_trades"] == 1
    assert kpis["losing_trades"] == 1
    assert kpis["win_rate"] == 50.0
    assert kpis["max_risk_cap"] == 1500.0


def test_read_only_wal_connection(tmp_path: Path):
    mock_db = tmp_path / "readonly_test.db"
    create_mock_journal_db(mock_db, populate_trades=True)

    conn = get_read_only_connection(mock_db)
    assert conn is not None

    # Verify write attempt raises OperationalError due to PRAGMA query_only = ON
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("INSERT INTO trade_journal (trade_id) VALUES ('FAIL')")

    conn.close()


@patch("streamlit.set_page_config")
@patch("streamlit.markdown")
@patch("streamlit.sidebar")
@patch("streamlit.columns")
@patch("streamlit.altair_chart")
@patch("streamlit.dataframe")
@patch("streamlit.download_button")
@patch("streamlit.info")
def test_dashboard_render_simulation(
    mock_info,
    mock_download,
    mock_df,
    mock_chart,
    mock_cols,
    mock_sidebar,
    mock_md,
    mock_config,
    tmp_path: Path,
):
    mock_cols.side_effect = lambda n: [MagicMock() for _ in range(n if isinstance(n, int) else len(n))]

    # 1. Render with populated DB
    mock_db = tmp_path / "render_test.db"
    create_mock_journal_db(mock_db, populate_trades=True)
    render_ui(journal_db_path=mock_db)
    assert mock_chart.called
    assert mock_df.called
    assert mock_download.called

    # 2. Render with empty DB
    empty_db = tmp_path / "render_empty.db"
    create_mock_journal_db(empty_db, populate_trades=False)
    render_ui(journal_db_path=empty_db)
    assert mock_info.called
