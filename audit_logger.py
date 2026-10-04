"""
Audit Logger Module for Algorithmic Trading Engine
Implements an ACID SQLite trade journal tracking:
- Maximum Adverse Excursion (MAE)
- Maximum Favorable Excursion (MFE)
- Execution slippage (entry and exit)
- Microsecond execution timestamps and trade lifecycle
- Daily performance summary and trade history export
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


@dataclass
class TradeAuditEntry:
    trade_id: str
    date: str
    symbol: str
    spread_type: str
    entry_time: str
    expected_entry_price: float
    actual_entry_price: float
    entry_slippage: float
    exit_time: Optional[str] = None
    expected_exit_price: Optional[float] = None
    actual_exit_price: Optional[float] = None
    exit_slippage: float = 0.0
    total_slippage: float = 0.0
    realized_pnl: float = 0.0
    mae_inr: float = 0.0  # Maximum Adverse Excursion (deepest negative point during trade)
    mfe_inr: float = 0.0  # Maximum Favorable Excursion (highest positive point during trade)
    is_paper: bool = True
    status: str = "OPEN"
    notes: str = ""


class AuditLogger:
    """
    SQLite-backed trade auditor and journal.
    Guarantees persistence of trade logs, execution slippage, MAE, and MFE.
    """

    def __init__(self, db_path: str | Path = "trading_journal.db", tz: ZoneInfo = IST):
        self.db_path = Path(db_path)
        self.tz = tz
        self._live_trackers: dict[str, dict[str, float]] = {}
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._get_connection() as conn:
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
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS execution_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trade_id TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    details TEXT,
                    FOREIGN KEY(trade_id) REFERENCES trade_journal(trade_id)
                );
                """
            )
            conn.commit()

    def log_trade_entry(
        self,
        trade_id: str,
        symbol: str,
        spread_type: str,
        expected_entry_price: float,
        actual_entry_price: float,
        is_paper: bool = True,
        timestamp: Optional[datetime] = None,
        notes: str = "",
    ) -> TradeAuditEntry:
        """
        Logs a trade entry event and starts real-time MAE/MFE tracking.
        """
        now = timestamp or datetime.now(self.tz)
        now_iso = now.isoformat()
        date_str = now.strftime("%Y-%m-%d")

        # Slippage: actual fill minus expected price
        entry_slippage = round(actual_entry_price - expected_entry_price, 4)

        entry = TradeAuditEntry(
            trade_id=trade_id,
            date=date_str,
            symbol=symbol,
            spread_type=spread_type,
            entry_time=now_iso,
            expected_entry_price=expected_entry_price,
            actual_entry_price=actual_entry_price,
            entry_slippage=entry_slippage,
            is_paper=is_paper,
            status="OPEN",
            notes=notes,
        )

        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO trade_journal (
                    trade_id, date, symbol, spread_type, entry_time,
                    expected_entry_price, actual_entry_price, entry_slippage,
                    is_paper, status, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry.trade_id,
                    entry.date,
                    entry.symbol,
                    entry.spread_type,
                    entry.entry_time,
                    entry.expected_entry_price,
                    entry.actual_entry_price,
                    entry.entry_slippage,
                    1 if entry.is_paper else 0,
                    entry.status,
                    entry.notes,
                ),
            )
            conn.execute(
                """
                INSERT INTO execution_events (trade_id, timestamp, event_type, details)
                VALUES (?, ?, 'ENTRY', ?)
                """,
                (trade_id, now_iso, f"Filled at {actual_entry_price}, slippage {entry_slippage}"),
            )
            conn.commit()

        # Initialize live MAE/MFE tracker (0.0 at inception)
        self._live_trackers[trade_id] = {"mae": 0.0, "mfe": 0.0}
        return entry

    def update_m2m(self, trade_id: str, current_pnl_inr: float) -> tuple[float, float]:
        """
        Updates live Maximum Adverse Excursion (MAE) and Maximum Favorable Excursion (MFE).
        MAE = minimum (most negative) PnL reached (expressed as a negative number or 0).
        MFE = maximum (most positive) PnL reached (expressed as positive number or 0).
        """
        if trade_id not in self._live_trackers:
            self._live_trackers[trade_id] = {"mae": 0.0, "mfe": 0.0}

        tracker = self._live_trackers[trade_id]
        if current_pnl_inr > tracker["mfe"]:
            tracker["mfe"] = current_pnl_inr
        if current_pnl_inr < tracker["mae"]:
            tracker["mae"] = current_pnl_inr

        return tracker["mae"], tracker["mfe"]

    def log_trade_exit(
        self,
        trade_id: str,
        expected_exit_price: float,
        actual_exit_price: float,
        realized_pnl: float,
        timestamp: Optional[datetime] = None,
        notes: str = "",
    ) -> TradeAuditEntry:
        """
        Logs trade exit, calculates exit slippage, closes live MAE/MFE tracking,
        and records final metrics into SQLite.
        """
        now = timestamp or datetime.now(self.tz)
        now_iso = now.isoformat()

        # Exit slippage
        exit_slippage = round(actual_exit_price - expected_exit_price, 4)

        # Retrieve tracked MAE/MFE
        tracker = self._live_trackers.pop(trade_id, {"mae": 0.0, "mfe": 0.0})
        # If realized PnL was higher/lower than previous ticks
        mae = min(tracker["mae"], min(0.0, realized_pnl))
        mfe = max(tracker["mfe"], max(0.0, realized_pnl))

        with self._get_connection() as conn:
            # Fetch existing entry
            row = conn.execute("SELECT * FROM trade_journal WHERE trade_id = ?", (trade_id,)).fetchone()
            if not row:
                raise ValueError(f"Trade with id '{trade_id}' not found in journal")

            entry_slippage = row["entry_slippage"]
            total_slippage = round(entry_slippage + exit_slippage, 4)

            conn.execute(
                """
                UPDATE trade_journal SET
                    exit_time = ?,
                    expected_exit_price = ?,
                    actual_exit_price = ?,
                    exit_slippage = ?,
                    total_slippage = ?,
                    realized_pnl = ?,
                    mae_inr = ?,
                    mfe_inr = ?,
                    status = 'CLOSED',
                    notes = CASE WHEN ? != '' THEN notes || ' | ' || ? ELSE notes END
                WHERE trade_id = ?
                """,
                (
                    now_iso,
                    expected_exit_price,
                    actual_exit_price,
                    exit_slippage,
                    total_slippage,
                    realized_pnl,
                    mae,
                    mfe,
                    notes,
                    notes,
                    trade_id,
                ),
            )
            conn.execute(
                """
                INSERT INTO execution_events (trade_id, timestamp, event_type, details)
                VALUES (?, ?, 'EXIT', ?)
                """,
                (
                    trade_id,
                    now_iso,
                    f"Closed with PnL {realized_pnl:.2f}, MAE {mae:.2f}, MFE {mfe:.2f}, total slippage {total_slippage}",
                ),
            )
            conn.commit()

            updated = conn.execute("SELECT * FROM trade_journal WHERE trade_id = ?", (trade_id,)).fetchone()
            return TradeAuditEntry(
                trade_id=updated["trade_id"],
                date=updated["date"],
                symbol=updated["symbol"],
                spread_type=updated["spread_type"],
                entry_time=updated["entry_time"],
                expected_entry_price=updated["expected_entry_price"],
                actual_entry_price=updated["actual_entry_price"],
                entry_slippage=updated["entry_slippage"],
                exit_time=updated["exit_time"],
                expected_exit_price=updated["expected_exit_price"],
                actual_exit_price=updated["actual_exit_price"],
                exit_slippage=updated["exit_slippage"],
                total_slippage=updated["total_slippage"],
                realized_pnl=updated["realized_pnl"],
                mae_inr=updated["mae_inr"],
                mfe_inr=updated["mfe_inr"],
                is_paper=bool(updated["is_paper"]),
                status=updated["status"],
                notes=updated["notes"],
            )

    def get_trade(self, trade_id: str) -> Optional[TradeAuditEntry]:
        with self._get_connection() as conn:
            row = conn.execute("SELECT * FROM trade_journal WHERE trade_id = ?", (trade_id,)).fetchone()
            if not row:
                return None
            return TradeAuditEntry(
                trade_id=row["trade_id"],
                date=row["date"],
                symbol=row["symbol"],
                spread_type=row["spread_type"],
                entry_time=row["entry_time"],
                expected_entry_price=row["expected_entry_price"],
                actual_entry_price=row["actual_entry_price"],
                entry_slippage=row["entry_slippage"],
                exit_time=row["exit_time"],
                expected_exit_price=row["expected_exit_price"],
                actual_exit_price=row["actual_exit_price"],
                exit_slippage=row["exit_slippage"],
                total_slippage=row["total_slippage"],
                realized_pnl=row["realized_pnl"],
                mae_inr=row["mae_inr"],
                mfe_inr=row["mfe_inr"],
                is_paper=bool(row["is_paper"]),
                status=row["status"],
                notes=row["notes"],
            )

    def get_daily_performance(self, date_str: Optional[str] = None) -> dict[str, Any]:
        """Calculates performance statistics for a given trading session."""
        target_date = date_str or datetime.now(self.tz).strftime("%Y-%m-%d")
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM trade_journal WHERE date = ? AND status = 'CLOSED'", (target_date,)
            ).fetchall()

            if not rows:
                return {
                    "date": target_date,
                    "trade_count": 0,
                    "total_pnl": 0.0,
                    "win_rate": 0.0,
                    "avg_mae": 0.0,
                    "avg_mfe": 0.0,
                    "avg_slippage": 0.0,
                }

            pnls = [r["realized_pnl"] for r in rows]
            maes = [r["mae_inr"] for r in rows]
            mfes = [r["mfe_inr"] for r in rows]
            slippages = [r["total_slippage"] for r in rows]
            wins = sum(1 for p in pnls if p > 0)

            return {
                "date": target_date,
                "trade_count": len(rows),
                "total_pnl": round(sum(pnls), 2),
                "wins": wins,
                "losses": len(rows) - wins,
                "win_rate": round((wins / len(rows)) * 100.0, 1),
                "avg_mae": round(sum(maes) / len(maes), 2),
                "avg_mfe": round(sum(mfes) / len(mfes), 2),
                "avg_slippage": round(sum(slippages) / len(slippages), 4),
            }
