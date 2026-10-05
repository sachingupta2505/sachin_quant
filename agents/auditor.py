"""
Auditor Agent
Role: Continuous Compliance, Risk FSM Guardian & Trade Auditor
Responsibilities:
1. Enforces Hard Risk FSM:
   - Max 2 trades per day limit.
   - -1,500 INR hard daily loss kill-switch.
   - 09:45 AM - 03:05 PM IST trading entry window.
   - 03:10 PM IST mandatory square-off trigger.
2. Intercepts PROPOSED_ORDER payloads from Coder Agent for compliance verification.
3. Emits AUDIT_APPROVED or AUDIT_REJECTED.
4. Maintains ACID SQLite trade journal with entry/exit slippage and real-time MAE/MFE tracking.
5. Monitors mark-to-market ticks and commands emergency liquidation if risk thresholds trip.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from agents.base import AgentMessage, BaseAgent, MessageType
from audit_logger import AuditLogger
from risk_guard import RiskGuard, RiskState

IST = ZoneInfo("Asia/Kolkata")
MAX_DAILY_LOSS_INR: float = -1500.0  # Hard daily loss kill-switch in INR
MAX_DAILY_TRADES: int = 2            # Hard limit: maximum 2 trades per day


class AuditorAgent(BaseAgent):
    def __init__(
        self,
        dispatch_fn: Optional[Callable[[AgentMessage], None]] = None,
        state_file: str = "daily_state.json",
        db_path: str = "trading_journal.db",
        tz: ZoneInfo = IST,
    ):
        super().__init__(name="Auditor")
        self.dispatch_fn = dispatch_fn
        self.tz = tz
        self.risk_guard = RiskGuard(state_file=state_file, tz=tz)
        self.audit_logger = AuditLogger(db_path=db_path, tz=tz)

    def handle_message(self, message: AgentMessage) -> None:
        if message.msg_type == MessageType.PROPOSED_ORDER:
            self.audit_proposed_order(message.payload)
        elif message.msg_type == MessageType.EXECUTION_CONFIRMATION:
            self.on_execution_confirmed(message.payload)
        elif message.msg_type == MessageType.POSITION_CLOSED:
            self.on_position_closed(message.payload)

    def audit_proposed_order(self, payload: dict) -> None:
        trade_id = payload["trade_id"]
        ts = payload.get("timestamp") or datetime.now(self.tz)
        legs = payload.get("legs", [])

        # 0. Enforce Nifty Lot Size = 65: order.quantity % 65 == 0
        for leg in legs:
            qty = int(leg.get("quantity", 0))
            if qty <= 0 or qty % 65 != 0:
                reason = f"Order leg quantity {qty} rejected: must strictly be a positive multiple of 65"
                self.logger.warning(f"[AUDIT VETO] Order {trade_id} REJECTED! Reason: {reason}")
                if self.dispatch_fn:
                    veto_msg = AgentMessage(
                        msg_id=f"REJ-{uuid.uuid4().hex[:6].upper()}",
                        sender=self.name,
                        recipient="Coder",
                        msg_type=MessageType.AUDIT_REJECTED,
                        payload={"trade_id": trade_id, "reason": reason, "timestamp": ts},
                        timestamp=ts,
                    )
                    self.dispatch_fn(veto_msg)
                return

        # Recalculate single-trade maximum loss with multiplier 65
        if len(legs) >= 2:
            buy_leg = next((l for l in legs if l.get("action") == "BUY"), legs[0])
            sell_leg = next((l for l in legs if l.get("action") == "SELL"), legs[1])
            qty = int(buy_leg.get("quantity", 65))
            spread_width = abs(float(buy_leg.get("strike", 0)) - float(sell_leg.get("strike", 0)))
            net_credit = float(payload.get("net_credit", 0.0))
            calculated_risk = round((spread_width * qty) - (net_credit * qty), 2)
            if calculated_risk > 1500.0:
                reason = f"Calculated max risk INR {calculated_risk:.2f} (multiplier {qty}) exceeds limit of INR 1500.0"
                self.logger.warning(f"[AUDIT VETO] Order {trade_id} REJECTED! Reason: {reason}")
                if self.dispatch_fn:
                    veto_msg = AgentMessage(
                        msg_id=f"REJ-{uuid.uuid4().hex[:6].upper()}",
                        sender=self.name,
                        recipient="Coder",
                        msg_type=MessageType.AUDIT_REJECTED,
                        payload={"trade_id": trade_id, "reason": reason, "timestamp": ts},
                        timestamp=ts,
                    )
                    self.dispatch_fn(veto_msg)
                return

        # 1. Enforce hard single-spread risk ceiling <= INR 1500
        max_risk_inr = float(payload.get("max_risk_inr", 0.0))
        if max_risk_inr > 1500.0:
            reason = f"Proposed max risk INR {max_risk_inr:.2f} exceeds hard limit of INR 1500.0"
            self.logger.warning(f"[AUDIT VETO] Order {trade_id} REJECTED! Reason: {reason}")
            if self.dispatch_fn:
                veto_msg = AgentMessage(
                    msg_id=f"REJ-{uuid.uuid4().hex[:6].upper()}",
                    sender=self.name,
                    recipient="Coder",
                    msg_type=MessageType.AUDIT_REJECTED,
                    payload={"trade_id": trade_id, "reason": reason, "timestamp": ts},
                    timestamp=ts,
                )
                self.dispatch_fn(veto_msg)
            return

        # 2. Hard daily loss limit invariant check (rejects if daily PnL <= -1500.0 INR)
        if self.risk_guard.total_pnl <= MAX_DAILY_LOSS_INR:
            reason = f"Hard daily loss limit breached: total PnL ₹{self.risk_guard.total_pnl:.2f} <= ₹{MAX_DAILY_LOSS_INR:.2f}"
            self.logger.warning(f"[AUDIT VETO] Order {trade_id} REJECTED! Reason: {reason}")
            if self.dispatch_fn:
                veto_msg = AgentMessage(
                    msg_id=f"REJ-{uuid.uuid4().hex[:6].upper()}",
                    sender=self.name,
                    recipient="Coder",
                    msg_type=MessageType.AUDIT_REJECTED,
                    payload={"trade_id": trade_id, "reason": reason, "timestamp": ts},
                    timestamp=ts,
                )
                self.dispatch_fn(veto_msg)
            return

        # 3. Maximum daily trades check (rejects if daily trade count >= 2)
        if self.risk_guard.trade_count >= MAX_DAILY_TRADES:
            reason = f"Maximum daily trade limit ({MAX_DAILY_TRADES}) reached: {self.risk_guard.trade_count} trades"
            self.logger.warning(f"[AUDIT VETO] Order {trade_id} REJECTED! Reason: {reason}")
            if self.dispatch_fn:
                veto_msg = AgentMessage(
                    msg_id=f"REJ-{uuid.uuid4().hex[:6].upper()}",
                    sender=self.name,
                    recipient="Coder",
                    msg_type=MessageType.AUDIT_REJECTED,
                    payload={"trade_id": trade_id, "reason": reason, "timestamp": ts},
                    timestamp=ts,
                )
                self.dispatch_fn(veto_msg)
            return

        # 4. Evaluate full FSM limits (timing gates, session state)
        allowed, reason = self.risk_guard.can_enter_trade(current_time=ts)

        if not allowed:
            self.logger.warning(
                f"[AUDIT VETO] Order {trade_id} REJECTED by RiskGuard! Reason: {reason}"
            )
            if self.dispatch_fn:
                veto_msg = AgentMessage(
                    msg_id=f"REJ-{uuid.uuid4().hex[:6].upper()}",
                    sender=self.name,
                    recipient="Coder",
                    msg_type=MessageType.AUDIT_REJECTED,
                    payload={"trade_id": trade_id, "reason": reason, "timestamp": ts},
                    timestamp=ts,
                )
                self.dispatch_fn(veto_msg)
            return

        # 2. Approved: Record planned entry in FSM
        self.risk_guard.record_trade_entry(
            trade_id=trade_id,
            details=payload,
            current_time=ts,
        )

        self.logger.info(
            f"[AUDIT APPROVED] Order {trade_id} complies with all risk boundaries. "
            f"Current Day Trades: {self.risk_guard.trade_count}/2 | "
            f"FSM State: {self.risk_guard.current_state.value}"
        )

        # 3. Dispatch approval to DevOps Agent for immediate broker routing
        if self.dispatch_fn:
            app_msg = AgentMessage(
                msg_id=f"APP-{uuid.uuid4().hex[:6].upper()}",
                sender=self.name,
                recipient="DevOps",
                msg_type=MessageType.AUDIT_APPROVED,
                payload=payload,
                timestamp=ts,
            )
            self.dispatch_fn(app_msg)

    def on_execution_confirmed(self, payload: dict) -> None:
        """Logs confirmed execution into ACID SQLite journal."""
        trade_id = payload["trade_id"]
        entry_price = float(payload.get("actual_entry_price", payload["net_credit"]))
        expected_price = float(payload.get("expected_entry_price", payload["net_credit"]))
        is_paper = bool(payload.get("is_paper", True))
        ts = payload.get("timestamp") or datetime.now(self.tz)

        entry = self.audit_logger.log_trade_entry(
            trade_id=trade_id,
            symbol="NIFTY",
            spread_type=payload["spread_type"],
            expected_entry_price=expected_price,
            actual_entry_price=entry_price,
            is_paper=is_paper,
            timestamp=ts,
            notes=payload.get("architect_signal", ""),
        )

        self.logger.info(
            f"[JOURNAL RECORDED] Trade {trade_id} committed to SQLite journal. "
            f"Slippage: {entry.entry_slippage} pts."
        )

    def audit_tick_m2m(self, trade_id: str, m2m_pnl: float, current_time: Optional[datetime] = None) -> None:
        """Audits live mark-to-market ticks against MAE/MFE and kill-switch."""
        now = current_time or datetime.now(self.tz)
        mae, mfe = self.audit_logger.update_m2m(trade_id, m2m_pnl)

        # Update FSM with live M2M
        state = self.risk_guard.update_unrealized_pnl(m2m_pnl, current_time=now)

        if state == RiskState.KILL_SWITCH_ACTIVE:
            self.logger.critical(
                f"[KILL SWITCH BREACHED] M2M INR {m2m_pnl:.2f} <= INR {self.risk_guard.total_pnl:.2f}! "
                f"Ordering emergency square-off!"
            )
            if self.dispatch_fn:
                alert = AgentMessage(
                    msg_id=f"KILL-{uuid.uuid4().hex[:6].upper()}",
                    sender=self.name,
                    recipient="DevOps",
                    msg_type=MessageType.SQUARE_OFF_ALERT,
                    payload={"trade_id": trade_id, "reason": "KILL_SWITCH_BREACHED", "timestamp": now},
                    timestamp=now,
                )
                self.dispatch_fn(alert)

    def on_position_closed(self, payload: dict) -> None:
        """Records exit in RiskGuard and completes SQLite journal entry."""
        trade_id = payload["trade_id"]
        realized_pnl = float(payload["realized_pnl"])
        expected_exit = float(payload.get("expected_exit_price", 0.0))
        actual_exit = float(payload.get("actual_exit_price", 0.0))
        ts = payload.get("timestamp") or datetime.now(self.tz)

        self.risk_guard.record_trade_exit(trade_id, realized_pnl=realized_pnl, current_time=ts)

        closed = self.audit_logger.log_trade_exit(
            trade_id=trade_id,
            expected_exit_price=expected_exit,
            actual_exit_price=actual_exit,
            realized_pnl=realized_pnl,
            timestamp=ts,
            notes=payload.get("exit_reason", ""),
        )

        self.logger.info(
            f"[TRADE CLOSED] {trade_id} | Realized PnL: INR {realized_pnl:.2f} | "
            f"MAE: INR {closed.mae_inr:.2f} | MFE: INR {closed.mfe_inr:.2f} | Total Slippage: {closed.total_slippage}"
        )
