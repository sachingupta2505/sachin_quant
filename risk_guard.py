"""
Risk Guard Module for Algorithmic Trading Engine
Implements a crash-resilient Finite State Machine (FSM) enforcing hard risk boundaries:
1. Hard daily loss kill-switch (-1,500 INR).
2. Max 2 trades per day limit.
3. Trading window gating (09:45 AM - 03:05 PM IST for entries, 03:10 PM square-off trigger).
4. State persistence to daily_state.json.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, time
from enum import Enum
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

# Hard risk limits
MAX_DAILY_LOSS_INR: float = -1500.0
MAX_DAILY_TRADES: int = 2

# Session timing constants (IST)
WINDOW_START_TIME: time = time(9, 45)      # 09:45 AM IST - Entry window opens
WINDOW_END_TIME: time = time(15, 5)        # 03:05 PM IST - No new entries after this
SQUARE_OFF_TIME: time = time(15, 10)       # 03:10 PM IST - Mandatory square-off
MARKET_CLOSE_TIME: time = time(15, 30)     # 03:30 PM IST - Market close
EXPIRY_ENTRY_CUTOFF_TIME: time = time(12, 30)  # 12:30 PM IST - Freeze fresh entries on expiry days
EXPIRY_SQUARE_OFF_TIME: time = time(13, 30)    # 01:30 PM IST - Mandatory square-off on expiry days


class RiskState(str, Enum):
    IDLE = "IDLE"                            # Outside market hours or pre-open
    READY = "READY"                          # Within window, limits healthy, ready to trade
    IN_TRADE = "IN_TRADE"                    # Active open position
    MAX_TRADES_REACHED = "MAX_TRADES_REACHED"# Limit of 2 trades exhausted
    KILL_SWITCH_ACTIVE = "KILL_SWITCH_ACTIVE"# Daily loss breach (<= -1500 INR)
    SQUARE_OFF_TRIGGERED = "SQUARE_OFF_TRIGGERED" # 03:10 PM IST reached or kill-switch liquidation
    DAY_CLOSED = "DAY_CLOSED"                # End of day session terminated


@dataclass
class TradeRecord:
    trade_id: str
    entry_time: str
    exit_time: Optional[str] = None
    realized_pnl: float = 0.0
    status: str = "OPEN"  # "OPEN" or "CLOSED"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class DailyRiskState:
    date: str                                # YYYY-MM-DD (IST)
    state: RiskState = RiskState.IDLE
    trade_count: int = 0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0
    kill_switch_triggered: bool = False
    kill_switch_reason: Optional[str] = None
    square_off_triggered: bool = False
    trades: list[dict[str, Any]] = field(default_factory=list)


class RiskGuard:
    """
    Finite State Machine and risk guard enforcing trading rules and persistence.
    """

    def __init__(self, state_file: str | Path = "daily_state.json", tz: ZoneInfo = IST):
        self.state_file = Path(state_file)
        self.tz = tz
        self._data: DailyRiskState = self._load_or_initialize()

    @property
    def current_state(self) -> RiskState:
        return self._data.state

    @property
    def total_pnl(self) -> float:
        return self._data.realized_pnl + self._data.unrealized_pnl

    @property
    def trade_count(self) -> int:
        return self._data.trade_count

    @property
    def realized_pnl(self) -> float:
        return self._data.realized_pnl

    @property
    def unrealized_pnl(self) -> float:
        return self._data.unrealized_pnl

    def get_current_time(self, custom_time: Optional[datetime] = None) -> datetime:
        if custom_time is not None:
            if custom_time.tzinfo is None:
                return custom_time.replace(tzinfo=self.tz)
            return custom_time.astimezone(self.tz)
        return datetime.now(self.tz)

    def _get_today_str(self, dt: Optional[datetime] = None) -> str:
        current = self.get_current_time(dt)
        return current.strftime("%Y-%m-%d")

    def _load_or_initialize(self) -> DailyRiskState:
        today = self._get_today_str()
        if self.state_file.exists():
            try:
                with open(self.state_file, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                if raw.get("date") == today:
                    return DailyRiskState(
                        date=raw["date"],
                        state=RiskState(raw.get("state", RiskState.IDLE.value)),
                        trade_count=int(raw.get("trade_count", 0)),
                        realized_pnl=float(raw.get("realized_pnl", 0.0)),
                        unrealized_pnl=float(raw.get("unrealized_pnl", 0.0)),
                        kill_switch_triggered=bool(raw.get("kill_switch_triggered", False)),
                        kill_switch_reason=raw.get("kill_switch_reason"),
                        square_off_triggered=bool(raw.get("square_off_triggered", False)),
                        trades=raw.get("trades", []),
                    )
            except (json.JSONDecodeError, KeyError, ValueError):
                # Corrupted state file fallback
                pass

        # Fresh state for today
        initial = DailyRiskState(date=today, state=RiskState.IDLE)
        self._atomic_save(initial)
        return initial

    def _atomic_save(self, state_to_save: Optional[DailyRiskState] = None) -> None:
        """Atomically saves state to disk to prevent corruptions during power/system crashes."""
        if state_to_save is None:
            state_to_save = self._data

        payload = {
            "date": state_to_save.date,
            "state": state_to_save.state.value,
            "trade_count": state_to_save.trade_count,
            "realized_pnl": round(state_to_save.realized_pnl, 2),
            "unrealized_pnl": round(state_to_save.unrealized_pnl, 2),
            "total_pnl": round(state_to_save.realized_pnl + state_to_save.unrealized_pnl, 2),
            "kill_switch_triggered": state_to_save.kill_switch_triggered,
            "kill_switch_reason": state_to_save.kill_switch_reason,
            "square_off_triggered": state_to_save.square_off_triggered,
            "trades": state_to_save.trades,
            "last_updated": datetime.now(self.tz).isoformat(),
        }

        parent = self.state_file.parent
        parent.mkdir(parents=True, exist_ok=True)

        temp_fd, temp_path = tempfile.mkstemp(dir=parent, prefix="state_", suffix=".tmp")
        try:
            with open(temp_fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, default=str)
            replaced = False
            for _ in range(5):
                try:
                    os.replace(temp_path, self.state_file)
                    replaced = True
                    break
                except (PermissionError, OSError):
                    time.sleep(0.05)
            if not replaced:
                with open(self.state_file, "w", encoding="utf-8") as f:
                    json.dump(payload, f, indent=2, default=str)
        finally:
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass

    def evaluate_fsm(self, current_time: Optional[datetime] = None) -> RiskState:
        """
        Evaluates state transitions based on time, PnL limits, and trade counts.
        """
        now = self.get_current_time(current_time)
        today = self._get_today_str(now)

        # Day rollover check
        if self._data.date != today:
            self._data = DailyRiskState(date=today, state=RiskState.IDLE)
            self._atomic_save()

        now_time = now.time()

        # 1. Hard Kill-switch priority: Daily loss breached
        if self.total_pnl <= MAX_DAILY_LOSS_INR:
            if not self._data.kill_switch_triggered:
                self._data.kill_switch_triggered = True
                self._data.kill_switch_reason = (
                    f"Daily loss limit breached: total PnL ₹{self.total_pnl:.2f} <= ₹{MAX_DAILY_LOSS_INR:.2f}"
                )
            self._data.state = RiskState.KILL_SWITCH_ACTIVE
            self._atomic_save()
            return self._data.state

        # If kill switch was already active, it cannot be exited for the rest of the day
        if self._data.kill_switch_triggered:
            self._data.state = RiskState.KILL_SWITCH_ACTIVE
            return self._data.state

        # 2. Time-based gating: Market close (>= 15:30)
        if now_time >= MARKET_CLOSE_TIME:
            self._data.state = RiskState.DAY_CLOSED
            self._atomic_save()
            return self._data.state

        # 3. Mandatory Square-off trigger (>= 13:30 on expiry days, >= 15:10 on normal days)
        is_expiry = now.weekday() in (1, 3)
        if is_expiry and now_time >= EXPIRY_SQUARE_OFF_TIME:
            self._data.square_off_triggered = True
            self._data.state = RiskState.SQUARE_OFF_TRIGGERED
            self._atomic_save()
            return self._data.state

        if now_time >= SQUARE_OFF_TIME:
            self._data.square_off_triggered = True
            self._data.state = RiskState.SQUARE_OFF_TRIGGERED
            self._atomic_save()
            return self._data.state

        # 4. If currently in trade, preserve IN_TRADE unless square-off or kill switch
        has_open_trade = any(t.get("status") == "OPEN" for t in self._data.trades)
        if has_open_trade:
            self._data.state = RiskState.IN_TRADE
            self._atomic_save()
            return self._data.state

        # 5. Max trades limit check (2 trades per day)
        if self._data.trade_count >= MAX_DAILY_TRADES:
            self._data.state = RiskState.MAX_TRADES_REACHED
            self._atomic_save()
            return self._data.state

        # 6. Entry window gating (09:45 AM - 03:05 PM on normal days, 09:45 AM - 12:30 PM on expiry days)
        effective_window_end = EXPIRY_ENTRY_CUTOFF_TIME if is_expiry else WINDOW_END_TIME
        if WINDOW_START_TIME <= now_time < effective_window_end:
            self._data.state = RiskState.READY
        else:
            self._data.state = RiskState.IDLE

        self._atomic_save()
        return self._data.state

    def can_enter_trade(self, current_time: Optional[datetime] = None) -> tuple[bool, str]:
        """
        Validates if a new entry order is permitted under all risk rules.
        Returns: (allowed: bool, reason: str)
        """
        state = self.evaluate_fsm(current_time)
        now = self.get_current_time(current_time)
        now_time = now.time()
        is_expiry = now.weekday() in (1, 3)

        if self._data.kill_switch_triggered:
            return False, f"Kill-switch active ({self._data.kill_switch_reason})"

        if self._data.trade_count >= MAX_DAILY_TRADES:
            return False, f"Maximum daily trade limit ({MAX_DAILY_TRADES}) reached"

        if state == RiskState.IN_TRADE:
            return False, "Active open trade exists; simultaneous trades not permitted"

        if now_time < WINDOW_START_TIME:
            return False, f"Before trading window start ({WINDOW_START_TIME.strftime('%H:%M')} IST)"

        if is_expiry and now_time >= EXPIRY_ENTRY_CUTOFF_TIME:
            return False, f"Expiry day entry cutoff: no new entries after {EXPIRY_ENTRY_CUTOFF_TIME.strftime('%H:%M')} IST"

        if now_time >= WINDOW_END_TIME:
            return False, f"Trading entry window closed at {WINDOW_END_TIME.strftime('%H:%M')} IST"

        if state != RiskState.READY:
            return False, f"System state is not READY: {state.value}"

        return True, "Trading conditions satisfied; order permitted"

    def record_trade_entry(
        self,
        trade_id: str,
        details: Optional[dict[str, Any]] = None,
        current_time: Optional[datetime] = None,
    ) -> None:
        """
        Records the opening of a new trade and transitions FSM to IN_TRADE.
        """
        allowed, reason = self.can_enter_trade(current_time)
        if not allowed:
            raise PermissionError(f"Trade entry rejected by RiskGuard: {reason}")

        now = self.get_current_time(current_time)
        new_trade = {
            "trade_id": trade_id,
            "entry_time": now.isoformat(),
            "exit_time": None,
            "realized_pnl": 0.0,
            "status": "OPEN",
            "metadata": details or {},
        }
        self._data.trades.append(new_trade)
        self._data.trade_count += 1
        self._data.state = RiskState.IN_TRADE
        self._atomic_save()

    def record_trade_exit(
        self,
        trade_id: str,
        realized_pnl: float,
        current_time: Optional[datetime] = None,
    ) -> None:
        """
        Records closing of an open trade, updates cumulative realized PnL,
        and re-evaluates risk state.
        """
        now = self.get_current_time(current_time)
        found = False

        for t in self._data.trades:
            if t["trade_id"] == trade_id and t["status"] == "OPEN":
                t["status"] = "CLOSED"
                t["exit_time"] = now.isoformat()
                t["realized_pnl"] = realized_pnl
                found = True
                break

        if not found:
            raise ValueError(f"Open trade with id '{trade_id}' not found")

        self._data.realized_pnl += realized_pnl
        self._data.unrealized_pnl = 0.0  # Reset unrealized on exit

        self.evaluate_fsm(now)
        self._atomic_save()

    def update_unrealized_pnl(
        self,
        unrealized_pnl: float,
        current_time: Optional[datetime] = None,
    ) -> RiskState:
        """
        Updates live unrealized PnL (from mark-to-market ticks).
        Triggers kill-switch immediately if total PnL drops below limit.
        """
        self._data.unrealized_pnl = unrealized_pnl
        return self.evaluate_fsm(current_time)

    def is_square_off_time(self, current_time: Optional[datetime] = None) -> bool:
        """
        Returns True if mandatory 03:10 PM IST square-off or kill switch is active.
        """
        state = self.evaluate_fsm(current_time)
        return state in (RiskState.SQUARE_OFF_TRIGGERED, RiskState.KILL_SWITCH_ACTIVE, RiskState.DAY_CLOSED)

    def get_summary(self) -> dict[str, Any]:
        """Returns clean snapshot dictionary for logging and status."""
        return {
            "date": self._data.date,
            "state": self._data.state.value,
            "trade_count": self._data.trade_count,
            "realized_pnl": self._data.realized_pnl,
            "unrealized_pnl": self._data.unrealized_pnl,
            "total_pnl": self.total_pnl,
            "kill_switch_triggered": self._data.kill_switch_triggered,
            "kill_switch_reason": self._data.kill_switch_reason,
            "square_off_triggered": self._data.square_off_triggered,
            "trades_count": len(self._data.trades),
        }
