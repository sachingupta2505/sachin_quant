"""
Coder Agent
Role: Quantitative Order Payload Synthesizer & Dynamic Risk Adjuster
Responsibilities:
1. Receives validated strategy signals from Architect Agent.
2. Dynamically calculates strike prices (ATM sell leg vs OTM hedge leg).
3. Adapts order payloads based on market conditions, lot sizes, and spread widths.
4. Formulates defined-risk 2-leg option spread structures.
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
DEFAULT_LOT_SIZE = 25  # Official NSE Nifty derivatives lot size
MAX_PERMITTED_SPREAD_RISK_INR = 1500.0  # Must align with daily loss kill-switch


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

        # Candidate strike widths to test (starting from desired down to minimum 50 pt boundary)
        candidate_widths = [self.spread_width]
        if self.spread_width > 50.0:
            candidate_widths.append(50.0)

        chosen_width: Optional[float] = None
        chosen_net_credit: float = 0.0
        chosen_max_risk_inr: float = 0.0

        for width in candidate_widths:
            # Model option pricing for paper/simulation
            sell_prem = 75.0
            # Deeper OTM hedge is cheaper; for 50 pt width ~57 pt, for larger width cheaper
            buy_prem = max(57.0 - (width - 50.0) * 0.25, 20.0)
            net_credit = sell_prem - buy_prem
            net_premium_inr = net_credit * self.lot_size
            risk_inr = (width * self.lot_size) - net_premium_inr

            if risk_inr <= MAX_PERMITTED_SPREAD_RISK_INR:
                chosen_width = width
                chosen_net_credit = net_credit
                chosen_max_risk_inr = round(risk_inr, 2)
                break
            else:
                self.logger.warning(
                    f"[RISK ADAPTATION] Width {width} pts yields Max Risk INR {risk_inr:.2f} > "
                    f"limit INR {MAX_PERMITTED_SPREAD_RISK_INR:.2f}. Attempting tighter strike width..."
                )

        # If even the tightest candidate breaches the 1500 INR ceiling, REJECT the trade formulation
        if chosen_width is None:
            self.logger.error(
                f"[TRADE FORMULATION REJECTED] Cannot construct defined-risk spread within "
                f"hard limit of INR {MAX_PERMITTED_SPREAD_RISK_INR:.2f} (Lot size: {self.lot_size}). "
                f"Trade proposal aborted."
            )
            return

        if signal_type_str == SignalType.BULLISH_REJECTION.value:
            # Bull Put Spread: Sell Put @ atm_strike, Buy Put @ atm_strike - chosen_width
            sell_strike = atm_strike
            buy_strike = sell_strike - chosen_width
            sell_prem = 75.0
            buy_prem = sell_prem - chosen_net_credit

            legs = [
                {
                    "symbol": f"NIFTY_{int(sell_strike)}_PE",
                    "strike": sell_strike,
                    "option_type": "PE",
                    "action": "SELL",
                    "quantity": self.lot_size,
                    "price": sell_prem,
                },
                {
                    "symbol": f"NIFTY_{int(buy_strike)}_PE",
                    "strike": buy_strike,
                    "option_type": "PE",
                    "action": "BUY",
                    "quantity": self.lot_size,
                    "price": buy_prem,
                },
            ]
            spread_type = SpreadType.BULL_PUT_SPREAD.value

        elif signal_type_str == SignalType.BEARISH_REJECTION.value:
            # Bear Call Spread: Sell Call @ atm_strike, Buy Call @ atm_strike + chosen_width
            sell_strike = atm_strike
            buy_strike = sell_strike + chosen_width
            sell_prem = 75.0
            buy_prem = sell_prem - chosen_net_credit

            legs = [
                {
                    "symbol": f"NIFTY_{int(sell_strike)}_CE",
                    "strike": sell_strike,
                    "option_type": "CE",
                    "action": "SELL",
                    "quantity": self.lot_size,
                    "price": sell_prem,
                },
                {
                    "symbol": f"NIFTY_{int(buy_strike)}_CE",
                    "strike": buy_strike,
                    "option_type": "CE",
                    "action": "BUY",
                    "quantity": self.lot_size,
                    "price": buy_prem,
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
            "max_risk_inr": chosen_max_risk_inr,
            "max_reward_inr": max_reward_inr,
            "timestamp": ts,
            "architect_signal": payload["description"],
        }

        self.logger.info(
            f"[PAYLOAD SYNTHESIZED] Proposed {spread_type} ({trade_id}) | "
            f"Width: {chosen_width} pts | Max Risk: INR {chosen_max_risk_inr} <= INR {MAX_PERMITTED_SPREAD_RISK_INR} | "
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
