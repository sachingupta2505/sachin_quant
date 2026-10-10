"""
Verification script for Dashboard Data Source and Parity.
Confirms that load_trades_data() against production trading_journal.db:
1. Returns exactly 1 trade for 2026-10-07 (Net +₹794.78).
2. Returns exactly 0 trades for 2026-10-08 (Zero fallback rows, ₹0.00 PnL).
3. Connects strictly to the production absolute path.
"""

from __future__ import annotations
import os
import sqlite3
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from dashboard import DEFAULT_JOURNAL_DB, load_trades_data, compute_kpis

def main():
    print("=" * 60)
    print("SACHINQUANT DASHBOARD DATA AUDIT & VERIFICATION")
    print("=" * 60)
    
    # 1. Database Path Verification
    db_path = DEFAULT_JOURNAL_DB.resolve()
    print(f"[PATH] Absolute Production DB Path: {db_path}")
    print(f"[PATH] File Exists: {db_path.exists()}")
    assert db_path.exists(), "Production DB not found!"

    # 2. Raw SQL Verification
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()

    cur.execute("SELECT trade_id, date, symbol, spread_type, actual_entry_price, actual_exit_price, gross_pnl, total_charges, net_pnl, status FROM trade_journal WHERE date = '2026-10-07'")
    raw_07 = cur.fetchall()
    print(f"\n[SQL 2026-10-07] Rows returned: {len(raw_07)}")
    for r in raw_07:
        print(f"  -> Trade ID: {r[0]} | Date: {r[1]} | Gross: ₹{r[6]:.2f} | Charges: ₹{r[7]:.2f} | Net PnL: ₹{r[8]:.2f} | Status: {r[9]}")

    cur.execute("SELECT * FROM trade_journal WHERE date = '2026-10-08'")
    raw_08 = cur.fetchall()
    print(f"\n[SQL 2026-10-08] Rows returned: {len(raw_08)}")

    cur.execute("SELECT COUNT(*) FROM trade_journal")
    total_db_count = cur.fetchone()[0]
    print(f"\n[SQL TOTAL] Total rows in database: {total_db_count}")
    conn.close()

    # 3. Dashboard Function Verification (load_trades_data & compute_kpis)
    print("\n" + "=" * 60)
    print("DASHBOARD FUNCTION EXECUTION RESULTS")
    print("=" * 60)

    # 2026-10-07
    df_07 = load_trades_data(db_path, target_date="2026-10-07")
    kpis_07 = compute_kpis(df_07)
    print(f"\nSession 2026-10-07:")
    print(f"  Total Trades:   {kpis_07['total_trades']}")
    print(f"  Gross PnL:      ₹{kpis_07['gross_pnl']:.2f}")
    print(f"  Total Charges:  ₹{kpis_07['total_charges']:.2f}")
    print(f"  Net Realized:   ₹{kpis_07['net_pnl']:.2f}")
    print(f"  Win Rate:       {kpis_07['win_rate']:.1f}%")
    if not df_07.empty:
        print(f"  Strikes:        {df_07.iloc[0]['strikes']}")
        print(f"  Strategy:       {df_07.iloc[0]['strategy_display']}")

    # 2026-10-08
    df_08 = load_trades_data(db_path, target_date="2026-10-08")
    kpis_08 = compute_kpis(df_08)
    print(f"\nSession 2026-10-08:")
    print(f"  Total Trades:   {kpis_08['total_trades']}")
    print(f"  Net Realized:   ₹{kpis_08['net_pnl']:.2f}")
    print(f"  Win Rate:       {kpis_08['win_rate']:.1f}%")
    print(f"  Is Empty DF:    {df_08.empty}")

    # All Sessions
    df_all = load_trades_data(db_path, target_date=None)
    kpis_all = compute_kpis(df_all)
    print(f"\nAll Recorded Sessions:")
    print(f"  Total Trades:   {kpis_all['total_trades']}")
    print(f"  Net Realized:   ₹{kpis_all['net_pnl']:.2f}")

    # Assertions
    assert len(df_07) == 1, f"Expected 1 trade for 2026-10-07, got {len(df_07)}"
    assert kpis_07['net_pnl'] == 794.78, f"Expected 794.78 Net PnL, got {kpis_07['net_pnl']}"
    assert len(df_08) == 0, f"Expected 0 trades for 2026-10-08, got {len(df_08)}"
    assert kpis_08['net_pnl'] == 0.0, f"Expected 0.0 Net PnL, got {kpis_08['net_pnl']}"
    assert total_db_count == 1, f"Expected total 1 trade in DB, got {total_db_count}"
    assert "NIFTY Spread" not in df_07.iloc[0]['strikes'], "Found fallback strike string!"
    assert "24900" not in df_07.iloc[0]['strikes'], "Found ghost strike!"

    print("\n" + "=" * 60)
    print("ALL AUDIT ASSERTIONS PASSED WITH 100% INTEGRITY")
    print("=" * 60)

if __name__ == "__main__":
    main()
