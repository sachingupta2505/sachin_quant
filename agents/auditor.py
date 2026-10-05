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
from datetime import datetime, time
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from agents.base import AgentMessage, BaseAgent, MessageType
from audit_logger import AuditLogger
from risk_guard import RiskGuard, RiskState
from agents.architect import is_expiry_day

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

    def on_proposed_order(self, payload: dict) -> None:
        """Alias for audit_proposed_order."""
        self.audit_proposed_order(payload)

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

        # Expiry Day Guard: Freeze fresh entries after 12:30 IST on expiry days
        if is_expiry_day(ts) and ts.time() >= time(12, 30):
            reason = f"Expiry Day Guard: Fresh entries frozen after 12:30 IST on expiry day (attempted at {ts.strftime('%H:%M:%S')})"
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

        # 1. Enforce Defined Stop-Loss Risk Ceiling <= INR 1500
        # Stop loss on spread is strictly capped at entry_credit * 2.0 (or max 20 pts adverse excursion)
        net_credit = float(payload.get("net_credit", 0.0))
        stop_loss_pts = float(payload.get("stop_loss_pts", 0.0))
        if stop_loss_pts <= 0.0 and net_credit > 0.0:
            stop_loss_pts = min(net_credit * 2.0, 20.0)

        qty = int(legs[0].get("quantity", 65)) if legs else 65
        stop_loss_risk = float(payload.get("stop_loss_risk_inr") or round(stop_loss_pts * qty, 2))
        stated_risk = float(payload.get("max_risk_inr", stop_loss_risk))
        effective_risk = stop_loss_risk if stop_loss_risk > 0 else stated_risk

        if effective_risk > 1500.0:
            reason = f"Defined stop-loss risk INR {effective_risk:.2f} (stop {stop_loss_pts:.1f} pts, multiplier {qty}) exceeds limit of INR 1500.0"
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

        # Expiry Day Mandatory Square-Off check (>= 13:30 IST on expiry days)
        if is_expiry_day(now) and now.time() >= time(13, 30):
            self.logger.warning(
                f"[EXPIRY MANDATORY SQUARE-OFF] Square-off mandated post-13:30 IST on expiry day for {trade_id}!"
            )
            if self.dispatch_fn:
                alert = AgentMessage(
                    msg_id=f"SQO-{uuid.uuid4().hex[:6].upper()}",
                    sender=self.name,
                    recipient="DevOps",
                    msg_type=MessageType.SQUARE_OFF_ALERT,
                    payload={"trade_id": trade_id, "reason": "EXPIRY_MANDATORY_SQUARE_OFF_1330", "timestamp": now},
                    timestamp=now,
                )
                self.dispatch_fn(alert)
            return

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
        realized_pnl = float(payload.get("realized_pnl", 0.0))
        gross_pnl = float(payload.get("gross_pnl", realized_pnl))
        total_charges = float(payload.get("total_charges", 0.0))
        net_pnl = float(payload.get("net_pnl", round(gross_pnl - total_charges, 2)))
        expected_exit = float(payload.get("expected_exit_price", 0.0))
        actual_exit = float(payload.get("actual_exit_price", 0.0))
        ts = payload.get("timestamp") or datetime.now(self.tz)

        self.risk_guard.record_trade_exit(trade_id, realized_pnl=net_pnl, current_time=ts)

        closed = self.audit_logger.log_trade_exit(
            trade_id=trade_id,
            expected_exit_price=expected_exit,
            actual_exit_price=actual_exit,
            realized_pnl=net_pnl,
            timestamp=ts,
            notes=payload.get("exit_reason", ""),
            gross_pnl=gross_pnl,
            total_charges=total_charges,
            net_pnl=net_pnl,
        )

        self.logger.info(
            f"[TRADE CLOSED] {trade_id} | Net PnL: INR {net_pnl:.2f} (Gross: {gross_pnl:.2f}, Charges: {total_charges:.2f}) | "
            f"MAE: INR {closed.mae_inr:.2f} | MFE: INR {closed.mfe_inr:.2f} | Total Slippage: {closed.total_slippage}"
        )
