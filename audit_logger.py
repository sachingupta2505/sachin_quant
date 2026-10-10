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
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Optional
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
    realized_pnl: float = 0.0  # Net realized PnL
    gross_pnl: float = 0.0     # Gross raw points * quantity
    total_charges: float = 0.0 # Total regulatory fees + brokerage
    net_pnl: float = 0.0       # gross_pnl - total_charges
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

    @contextmanager
    def _get_connection(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA busy_timeout = 5000;")

        # Invariant: NO DELETE or DROP permitted on production trading_journal.db
        prod_path = (Path(__file__).resolve().parent / "trading_journal.db").resolve()
        try:
            if self.db_path.resolve() == prod_path:
                def _guard_authorizer(action, arg1, arg2, dbname, source):
                    if action in (
                        sqlite3.SQLITE_DELETE,
                        sqlite3.SQLITE_DROP_TABLE,
                        sqlite3.SQLITE_DROP_INDEX,
                        sqlite3.SQLITE_DROP_VIEW,
                    ):
                        return sqlite3.SQLITE_DENY
                    return sqlite3.SQLITE_OK

                conn.set_authorizer(_guard_authorizer)
        except Exception:
            pass

        try:
            yield conn
        finally:
            conn.close()

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
                    gross_pnl REAL DEFAULT 0.0,
                    total_charges REAL DEFAULT 0.0,
                    net_pnl REAL DEFAULT 0.0,
                    mae_inr REAL DEFAULT 0.0,
                    mfe_inr REAL DEFAULT 0.0,
                    is_paper INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    notes TEXT
                );
                """
            )
            # Schema migration for existing databases missing new columns
            existing_cols = {col[1] for col in conn.execute("PRAGMA table_info(trade_journal)").fetchall()}
            for col_name in ("gross_pnl", "total_charges", "net_pnl"):
                if col_name not in existing_cols:
                    conn.execute(f"ALTER TABLE trade_journal ADD COLUMN {col_name} REAL DEFAULT 0.0;")

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
        realized_pnl: float = 0.0,
        timestamp: Optional[datetime] = None,
        notes: str = "",
        gross_pnl: Optional[float] = None,
        total_charges: Optional[float] = None,
        net_pnl: Optional[float] = None,
    ) -> TradeAuditEntry:
        """
        Logs trade exit, calculates exit slippage, closes live MAE/MFE tracking,
        and records final metrics into SQLite.
        """
        now = timestamp or datetime.now(self.tz)
        now_iso = now.isoformat()

        # Compute Gross, Charges, and Net PnL
        if gross_pnl is None:
            gross_pnl = realized_pnl
        if total_charges is None:
            total_charges = 0.0
        if net_pnl is None:
            net_pnl = round(gross_pnl - total_charges, 2)
        realized_pnl = net_pnl

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
                    gross_pnl = ?,
                    total_charges = ?,
                    net_pnl = ?,
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
                    gross_pnl,
                    total_charges,
                    net_pnl,
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
                    f"Closed with Net PnL {net_pnl:.2f} (Gross {gross_pnl:.2f}, Charges {total_charges:.2f}), MAE {mae:.2f}, MFE {mfe:.2f}, total slippage {total_slippage}",
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
                gross_pnl=updated["gross_pnl"] if "gross_pnl" in updated.keys() else 0.0,
                total_charges=updated["total_charges"] if "total_charges" in updated.keys() else 0.0,
                net_pnl=updated["net_pnl"] if "net_pnl" in updated.keys() else 0.0,
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
                gross_pnl=row["gross_pnl"] if "gross_pnl" in row.keys() else 0.0,
                total_charges=row["total_charges"] if "total_charges" in row.keys() else 0.0,
                net_pnl=row["net_pnl"] if "net_pnl" in row.keys() else 0.0,
                mae_inr=row["mae_inr"],
                mfe_inr=row["mfe_inr"],
                is_paper=bool(row["is_paper"]),
                status=row["status"],
                notes=row["notes"],
            )

    def reconcile_and_close_open_positions(
        self,
        exit_time: Optional[datetime] = None,
        notes: str = "EOD Auto Square-Off",
        lot_size: int = 65,
    ) -> list[TradeAuditEntry]:
        """
        Scans trade_journal for any trades with status='OPEN' and performs EOD square-off:
        - Sets actual_exit_price to 0.0 (worthless expiration for OTM credit spreads) or market exit.
        - Calculates points gained: entry_credit - exit_credit.
        - Calculates gross PnL = pts_gain * lot_size.
        - Calculates exact statutory Indian regulatory charges via IndianRegulatoryFeeCalculator.
        - Sets net_pnl = gross_pnl - total_charges.
        - Updates trade_journal row to status='CLOSED' and logs EXIT execution_event.
        """
        now = exit_time or datetime.now(self.tz)
        closed_entries: list[TradeAuditEntry] = []
        with self._get_connection() as conn:
            open_rows = conn.execute("SELECT trade_id FROM trade_journal WHERE status = 'OPEN'").fetchall()
            open_ids = [r["trade_id"] for r in open_rows]

        if not open_ids:
            return closed_entries

        from fee_calculator import IndianRegulatoryFeeCalculator
        fee_calc = IndianRegulatoryFeeCalculator()

        for tid in open_ids:
            trade = self.get_trade(tid)
            if not trade or trade.status != "OPEN":
                continue

            # For credit spreads held to EOD / expiry, default exit is 0.0 (full credit captured)
            exit_price = 0.0
            pts_gain = round(trade.actual_entry_price - exit_price, 4)
            gross_pnl = round(pts_gain * lot_size, 2)

            # Calculate statutory regulatory charges (2 executed legs for expired spread)
            sell_prem = max(10.0, round(trade.actual_entry_price * 1.9, 1))
            buy_prem = max(2.0, round(sell_prem - trade.actual_entry_price, 1))
            total_charges = fee_calc.calculate_charges(
                buy_premium=buy_prem,
                sell_premium=sell_prem,
                quantity=lot_size,
                num_legs=2,
            )
            net_pnl = round(gross_pnl - total_charges, 2)

            entry_exit = self.log_trade_exit(
                trade_id=tid,
                expected_exit_price=exit_price,
                actual_exit_price=exit_price,
                realized_pnl=net_pnl,
                timestamp=now,
                notes=notes,
                gross_pnl=gross_pnl,
                total_charges=total_charges,
                net_pnl=net_pnl,
            )
            closed_entries.append(entry_exit)

        return closed_entries

    def reconcile_eod(
        self,
        date_str: Optional[str] = None,
        broker_pnl: Optional[float] = None,
        lot_size: int = 65,
    ) -> dict[str, Any]:
        """
        Reconciles daily trades and PnL against broker/clearing house settlement:
        1. Ensures all open positions are closed via reconcile_and_close_open_positions.
        2. Calculates daily performance across all trades.
        3. Flags any discrepancies against broker_pnl if provided.
        """
        target_date = date_str or datetime.now(self.tz).strftime("%Y-%m-%d")
        closed_entries = self.reconcile_and_close_open_positions(notes=f"EOD Settlement {target_date}", lot_size=lot_size)
        perf = self.get_daily_performance(target_date)

        discrepancy = 0.0
        is_reconciled = True
        if broker_pnl is not None:
            discrepancy = round(perf["total_net_pnl"] - broker_pnl, 2)
            is_reconciled = abs(discrepancy) < 1.0  # within 1 INR tolerance

        perf["broker_pnl"] = broker_pnl
        perf["discrepancy"] = discrepancy
        perf["is_reconciled"] = is_reconciled
        perf["newly_closed_count"] = len(closed_entries)
        return perf

    def get_daily_performance(self, date_str: Optional[str] = None) -> dict[str, Any]:
        """Calculates performance statistics for a given trading session, tracking total, closed, and open trades."""
        target_date = date_str or datetime.now(self.tz).strftime("%Y-%m-%d")
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM trade_journal WHERE date = ? OR entry_time LIKE ?",
                (target_date, f"{target_date}%"),
            ).fetchall()

            if not rows:
                return {
                    "date": target_date,
                    "trade_count": 0,
                    "total_trades": 0,
                    "closed_trades": 0,
                    "open_trades": 0,
                    "total_pnl": 0.0,
                    "total_net_pnl": 0.0,
                    "total_gross_pnl": 0.0,
                    "total_charges": 0.0,
                    "wins": 0,
                    "losses": 0,
                    "win_rate": 0.0,
                    "avg_mae": 0.0,
                    "avg_mfe": 0.0,
                    "avg_slippage": 0.0,
                }

            closed_rows = [r for r in rows if r["status"] == "CLOSED"]
            open_rows = [r for r in rows if r["status"] == "OPEN"]

            pnls = [r["realized_pnl"] for r in closed_rows]
            gross_pnls = [r["gross_pnl"] if "gross_pnl" in r.keys() else r["realized_pnl"] for r in closed_rows]
            charges = [r["total_charges"] if "total_charges" in r.keys() else 0.0 for r in closed_rows]
            maes = [r["mae_inr"] for r in closed_rows] if closed_rows else [0.0]
            mfes = [r["mfe_inr"] for r in closed_rows] if closed_rows else [0.0]
            slippages = [r["total_slippage"] for r in closed_rows] if closed_rows else [0.0]
            wins = sum(1 for p in pnls if p > 0)

            total_pnl = round(sum(pnls), 2)
            total_gross = round(sum(gross_pnls), 2)
            total_chg = round(sum(charges), 2)
            win_rate = round((wins / len(closed_rows)) * 100.0, 1) if closed_rows else 0.0

            return {
                "date": target_date,
                "trade_count": len(rows),
                "total_trades": len(rows),
                "closed_trades": len(closed_rows),
                "open_trades": len(open_rows),
                "total_pnl": total_pnl,
                "total_net_pnl": total_pnl,
                "total_gross_pnl": total_gross,
                "total_charges": total_chg,
                "wins": wins,
                "losses": len(closed_rows) - wins,
                "win_rate": win_rate,
                "avg_mae": round(sum(maes) / len(maes), 2) if maes else 0.0,
                "avg_mfe": round(sum(mfes) / len(mfes), 2) if mfes else 0.0,
                "avg_slippage": round(sum(slippages) / len(slippages), 4) if slippages else 0.0,
            }
