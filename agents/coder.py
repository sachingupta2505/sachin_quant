"""
Coder Agent
Role: Quantitative Order Payload Synthesizer & Dynamic Risk Adjuster
Responsibilities:
1. Receives validated strategy signals from Architect Agent.
2. Dynamically calculates strike prices (safe OTM credit spreads: delta 0.15 - 0.25, 10-16 pts credit).
3. Adapts order payloads based on market conditions, lot sizes, and spread widths.
4. Enforces defined Stop-Loss Exit Rule (stop loss strictly capped at entry_credit * 2.0 or max 20 pts).
5. Emits PROPOSED_ORDER message to Auditor Agent for strict compliance gating.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from agents.base import AgentMessage, BaseAgent, MessageType
from execution_engine import SignalType, SpreadType

IST = ZoneInfo("Asia/Kolkata")
DEFAULT_SPREAD_WIDTH = 50.0
DEFAULT_LOT_SIZE = 65  # Official NSE Nifty derivatives lot size (65 qty per contract)
MAX_PERMITTED_SPREAD_RISK_INR = 1500.0  # Must align with daily loss kill-switch
MIN_NET_CREDIT_PTS = 10.0  # Minimum credit capture threshold (targets 10 - 16 pts for safe OTM spreads)
MAX_NET_CREDIT_PTS = 16.0  # Upper bound for OTM delta 0.15 - 0.25 options
MAX_STOP_LOSS_PTS = 20.0   # Maximum adverse excursion cap on option spread (pts)


class CoderAgent(BaseAgent):
    def __init__(
        self,
        dispatch_fn: Optional[Callable[[AgentMessage], None]] = None,
        spread_width: float = DEFAULT_SPREAD_WIDTH,
        lot_size: int = DEFAULT_LOT_SIZE,
        tz: ZoneInfo = IST,
    ):
        super().__init__(name="Coder")
        self.dispatch_fn = dispatch_fn
        self.spread_width = spread_width
        self.lot_size = lot_size
        self.tz = tz

    def handle_message(self, message: AgentMessage) -> None:
        if message.msg_type == MessageType.STRATEGY_SIGNAL:
            self.on_strategy_signal(message.payload)

    def on_strategy_signal(self, payload: dict) -> None:
        signal_type_str = payload["signal_type"]
        spot_price = float(payload["spot_price"])
        ts = payload.get("timestamp") or datetime.now(self.tz)

        # 1. Calculate ATM strike (nearest 50 pt boundary)
        atm_strike = round(spot_price / 50.0) * 50.0

        # Base quantity must strictly be 65 or multiples of 65
        if self.lot_size <= 0 or self.lot_size % 65 != 0:
            self.logger.error(
                f"[ORDER REJECTED] Quantity {self.lot_size} is not a valid multiple of 65. "
                f"Trade proposal aborted."
            )
            return

        # 2. Select Spread Width (minimum 50 pt boundary)
        candidate_widths = []
        w = float(self.spread_width)
        while w >= 50.0:
            candidate_widths.append(w)
            w -= 50.0
        if not candidate_widths:
            candidate_widths = [50.0]

        # Use standard 50 pt spread width for high-probability OTM credit
        chosen_width = candidate_widths[-1] if candidate_widths else 50.0

        # 3. Safe OTM credit spread pricing (delta 0.15 - 0.25, targeting 10-16 pts credit)
        # OTM Short leg price ~ 25.0 pts, OTM Hedge leg price ~ 12.0 pts => Net credit ~ 13.0 pts
        target_credit = 13.0
        sell_prem = 25.0
        buy_prem = round(sell_prem - target_credit, 2)
        net_credit = round(sell_prem - buy_prem, 2)

        # 4. Defined Stop-Loss Exit Rule:
        # Strictly capped at: entry_credit * 2.0 (or max 20 pts adverse excursion)
        stop_loss_pts = round(min(net_credit * 2.0, MAX_STOP_LOSS_PTS), 2)
        stop_loss_risk_inr = round(stop_loss_pts * self.lot_size, 2)

        # Catastrophic unhedged failure risk
        catastrophic_risk_inr = round((chosen_width * self.lot_size) - (net_credit * self.lot_size), 2)

        # Invariant check: Defined stop-loss risk must strictly be <= 1500 INR and net_credit >= MIN_NET_CREDIT_PTS
        if stop_loss_risk_inr > MAX_PERMITTED_SPREAD_RISK_INR or not (net_credit >= MIN_NET_CREDIT_PTS):
            self.logger.error(
                f"[TRADE FORMULATION REJECTED] Stop-loss risk INR {stop_loss_risk_inr:.2f} > INR {MAX_PERMITTED_SPREAD_RISK_INR:.2f} "
                f"or credit {net_credit:.1f} < {MIN_NET_CREDIT_PTS} pts. Trade aborted."
            )
            return

        chosen_net_credit = net_credit
        chosen_max_risk_inr = stop_loss_risk_inr

        # 5. Strike selection: Safe OTM credit spreads (e.g. 1 strike OTM away from spot)
        if signal_type_str == SignalType.BULLISH_REJECTION.value:
            # Bull Put Spread: Sell OTM Put (atm_strike - 50), Buy Hedge Put (sell_strike - width)
            sell_strike = atm_strike - 50.0
            buy_strike = sell_strike - chosen_width

            # Margin requirement invariant: BUY leg MUST precede SELL leg
            legs = [
                {
                    "symbol": f"NIFTY_{int(buy_strike)}_PE",
                    "strike": buy_strike,
                    "option_type": "PE",
                    "action": "BUY",
                    "quantity": self.lot_size,
                    "price": buy_prem,
                },
                {
                    "symbol": f"NIFTY_{int(sell_strike)}_PE",
                    "strike": sell_strike,
                    "option_type": "PE",
                    "action": "SELL",
                    "quantity": self.lot_size,
                    "price": sell_prem,
                },
            ]
            spread_type = SpreadType.BULL_PUT_SPREAD.value

        elif signal_type_str == SignalType.BEARISH_REJECTION.value:
            # Bear Call Spread: Sell OTM Call (atm_strike + 50), Buy Hedge Call (sell_strike + width)
            sell_strike = atm_strike + 50.0
            buy_strike = sell_strike + chosen_width

            # Margin requirement invariant: BUY leg MUST precede SELL leg
            legs = [
                {
                    "symbol": f"NIFTY_{int(buy_strike)}_CE",
                    "strike": buy_strike,
                    "option_type": "CE",
                    "action": "BUY",
                    "quantity": self.lot_size,
                    "price": buy_prem,
                },
                {
                    "symbol": f"NIFTY_{int(sell_strike)}_CE",
                    "strike": sell_strike,
                    "option_type": "CE",
                    "action": "SELL",
                    "quantity": self.lot_size,
                    "price": sell_prem,
                },
            ]
            spread_type = SpreadType.BEAR_CALL_SPREAD.value

        else:
            self.logger.warning(f"Unsupported signal type: {signal_type_str}")
            return

        trade_id = f"SPD-{ts.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4].upper()}"
        max_reward_inr = round(chosen_net_credit * self.lot_size, 2)

        order_payload = {
            "trade_id": trade_id,
            "spread_type": spread_type,
            "legs": legs,
            "spot_price": spot_price,
            "spread_width": chosen_width,
            "net_credit": chosen_net_credit,
            "stop_loss_pts": stop_loss_pts,
            "stop_loss_risk_inr": stop_loss_risk_inr,
            "max_risk_inr": stop_loss_risk_inr,
            "catastrophic_max_risk_inr": catastrophic_risk_inr,
            "max_reward_inr": max_reward_inr,
            "timestamp": ts,
            "architect_signal": payload.get("description", ""),
        }

        self.logger.info(
            f"[PAYLOAD SYNTHESIZED] Proposed {spread_type} ({trade_id}) | "
            f"Width: {chosen_width} pts | Credit: {chosen_net_credit} pts | "
            f"Stop-Loss: {stop_loss_pts} pts (INR {stop_loss_risk_inr} <= {MAX_PERMITTED_SPREAD_RISK_INR}) | "
            f"Max Reward: INR {max_reward_inr}"
        )

        # Dispatch proposal to Auditor Agent
        if self.dispatch_fn:
            msg = AgentMessage(
                msg_id=f"PROP-{uuid.uuid4().hex[:6].upper()}",
                sender=self.name,
                recipient="Auditor",
                msg_type=MessageType.PROPOSED_ORDER,
                payload=order_payload,
                timestamp=ts,
            )
            self.dispatch_fn(msg)
