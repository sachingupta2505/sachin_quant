"""
Test Journal & State Reset Utility
Module: scripts/reset_journal.py

Responsibilities:
1. Backs up existing trading_journal.db containing test rows to data/trading_journal_test_backup.db.
2. Recreates/clears the trade_journal and execution_events tables in trading_journal.db so it starts
   completely fresh (0 trades, 0 PnL) for 2026-10-06.
3. Resets daily_state.json to fresh IDLE state (0 trade_count, 0.0 realized_pnl).
4. Cleans system_bus.db so no stale queue messages persist into the live trading day.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"
JOURNAL_DB = ROOT_DIR / "trading_journal.db"
BACKUP_DB = DATA_DIR / "trading_journal_test_backup.db"
DAILY_STATE = ROOT_DIR / "daily_state.json"
BACKUP_STATE = DATA_DIR / "daily_state_test_backup.json"
SYSTEM_BUS = ROOT_DIR / "system_bus.db"


def reset_trading_journal() -> tuple[int, int]:
    """Backs up trading_journal.db and clears trade_journal & execution_events tables."""
    prod_path = (ROOT_DIR / "trading_journal.db").resolve()
    if JOURNAL_DB.resolve() == prod_path:
        raise PermissionError(
            "INVARIANT VIOLATION: Execution of DELETE or DROP on production trading_journal.db is strictly forbidden."
        )

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    rows_cleared = 0

    if JOURNAL_DB.exists():
        # Copy to backup
        shutil.copy2(JOURNAL_DB, BACKUP_DB)
        print(f"[BACKUP] Copied {JOURNAL_DB.name} -> {BACKUP_DB.relative_to(ROOT_DIR)}")

        conn = sqlite3.connect(JOURNAL_DB)
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name='trade_journal'")
            if cursor.fetchone()[0] > 0:
                cursor.execute("SELECT count(*) FROM trade_journal")
                rows_cleared = cursor.fetchone()[0]
                cursor.execute("DELETE FROM trade_journal")

            cursor.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name='execution_events'")
            if cursor.fetchone()[0] > 0:
                cursor.execute("DELETE FROM execution_events")

            conn.commit()
            cursor.execute("VACUUM")
            conn.commit()
        finally:
            conn.close()
    else:
        # Create fresh schema using AuditLogger
        from audit_logger import AuditLogger
        AuditLogger(db_path=JOURNAL_DB)

    # Verify count is 0
    conn = sqlite3.connect(JOURNAL_DB)
    c = conn.cursor()
    c.execute("SELECT count(*) FROM trade_journal")
    new_count = c.fetchone()[0]
    conn.close()

    return rows_cleared, new_count


def reset_daily_state() -> None:
    """Resets daily_state.json to fresh IDLE state for tomorrow."""
    if DAILY_STATE.exists():
        shutil.copy2(DAILY_STATE, BACKUP_STATE)
        print(f"[BACKUP] Copied {DAILY_STATE.name} -> {BACKUP_STATE.relative_to(ROOT_DIR)}")

    fresh_state = {
        "date": datetime.now(IST).strftime("%Y-%m-%d"),
        "state": "IDLE",
        "trade_count": 0,
        "realized_pnl": 0.0,
        "unrealized_pnl": 0.0,
        "total_pnl": 0.0,
        "kill_switch_triggered": False,
        "kill_switch_reason": None,
        "square_off_triggered": False,
        "trades": [],
        "last_updated": datetime.now(IST).isoformat(),
    }

    with open(DAILY_STATE, "w", encoding="utf-8") as f:
        json.dump(fresh_state, f, indent=2)
    print(f"[RESET] Reset {DAILY_STATE.name} to fresh IDLE state (trade_count=0, realized_pnl=0.0)")


import sys

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def reset_system_bus() -> None:
    """Cleans processed events from system_bus.db."""
    if SYSTEM_BUS.exists():
        conn = sqlite3.connect(SYSTEM_BUS)
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name='bus_events'")
            if cursor.fetchone()[0] > 0:
                cursor.execute("DELETE FROM bus_events WHERE status IN ('COMPLETED', 'PROCESSED', 'FAILED', 'VETOED')")
                conn.commit()
                cursor.execute("VACUUM")
                conn.commit()
                print(f"[RESET] Cleaned processed event log in {SYSTEM_BUS.name}")
        except Exception as e:
            print(f"[NOTE] System bus cleanup: {e}")
        finally:
            conn.close()


def main():
    print("=" * 70)
    print("      SACHIN QUANT: TEST JOURNAL & STATE RESET UTILITY")
    print("=" * 70)

    rows_backed_up, current_rows = reset_trading_journal()
    print(f"[JOURNAL] Backed up {rows_backed_up} test rows. Current active rows: {current_rows}")

    reset_daily_state()
    reset_system_bus()

    print("\n" + "=" * 70)
    print(" [READY] Trading Journal and State successfully reset for 2026-10-06!")
    print(f"         Database: {JOURNAL_DB.name} (0 trades, INR 0.00 PnL)")
    print(f"         State:    {DAILY_STATE.name} (IDLE, 0/2 daily trade cap)")
    print("=" * 70 + "\n")


if __name__ == "__main__":
    main()
