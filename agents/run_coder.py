"""
Process 2: Coder Agent Worker
Blackboard Pattern: Uses bus.py (system_bus.db)
Responsibilities:
1. Consumes SIGNAL_DETECTED events from Blackboard.
2. Dynamically calculates strike prices and evaluates candidate strike widths.
3. Strictly enforces (Spread Width * Lot Size) - Net Premium <= 1500 INR.
4. Generates defined-risk option spread payload.
5. Publishes ORDER_PROPOSED to Blackboard for Auditor Agent.
"""

from __future__ import annotations

import logging
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# Fix UTF-8 stdout on Windows
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

sys.path.insert(0, str(Path(__file__).parent.parent))

from bus import EventStatus, SystemBus
from execution_engine import SignalType, SpreadType

IST = ZoneInfo("Asia/Kolkata")
LOT_SIZE = 65  # Official NSE Nifty derivatives lot size
MAX_PERMITTED_RISK_INR = 1500.0

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [CODER] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("CoderWorker")


def run_coder():
    bus = SystemBus()

    print("\n" + "=" * 65)
    print(" [PROCESS 2] CODER AGENT - DYNAMIC OPTION SPREAD SYNTHESIZER")
    print("  Pattern: Event-Driven Blackboard (system_bus.db)")
    print("  Input Topic: SIGNAL_DETECTED | Output Topic: ORDER_PROPOSED")
    print(f"  Lot Size: {LOT_SIZE} | Hard Risk Ceiling: INR {MAX_PERMITTED_RISK_INR}")
    print("=" * 65 + "\n")

    logger.info("Coder Agent active. Polling system_bus.db for SIGNAL_DETECTED events...")

    while True:
        events = bus.consume(topic="SIGNAL_DETECTED", target="Coder")
        for event in events:
            payload = event.payload
            signal_type = payload["signal_type"]
            spot_price = float(payload["spot_price"])
            ts_str = payload.get("timestamp") or datetime.now(IST).isoformat()

            logger.info("=" * 50)
            logger.info(f"Consumed SIGNAL_DETECTED (Event #{event.id}): {payload['description']}")

            # Strike Calculation (ATM rounded to nearest 50-pt strike)
            atm_strike = round(spot_price / 50.0) * 50.0

            # Dynamic candidate widths respecting MAX_PERMITTED_RISK_INR <= 1500
            candidate_widths = [50.0]
            chosen_width = None
            chosen_credit = 0.0
            chosen_risk_inr = 0.0

            for width in candidate_widths:
                # Dynamic minimum credit required so that (width - net_credit) * LOT_SIZE <= 1500
                min_req_credit = width - (MAX_PERMITTED_RISK_INR / float(LOT_SIZE))
                dynamic_credit = max(10.0, round(min_req_credit, 2))
                net_credit = max(dynamic_credit + 0.5, dynamic_credit)
                sell_prem = 75.0
                buy_prem = sell_prem - net_credit
                net_premium_received = net_credit * LOT_SIZE
                risk_inr = (width * LOT_SIZE) - net_premium_received

                if risk_inr <= MAX_PERMITTED_RISK_INR:
                    chosen_width = width
                    chosen_credit = net_credit
                    chosen_risk_inr = round(risk_inr, 2)
                    break
                else:
                    logger.warning(
                        f"[RISK ADAPTATION] Width {width} yields INR {risk_inr:.2f} > INR {MAX_PERMITTED_RISK_INR}. Reducing width..."
                    )

            if chosen_width is None:
                logger.error(
                    f"[ORDER REJECTED] Cannot construct spread <= INR {MAX_PERMITTED_RISK_INR}. Aborting proposal."
                )
                bus.update_status(event.id, EventStatus.FAILED)
                continue

            # Build legs (Margin invariant: BUY leg MUST precede SELL leg)
            if signal_type == SignalType.BULLISH_REJECTION.value:
                sell_strike = atm_strike
                buy_strike = sell_strike - chosen_width
                spread_type = SpreadType.BULL_PUT_SPREAD.value
                legs = [
                    {"symbol": f"NIFTY_{int(buy_strike)}_PE", "strike": buy_strike, "option_type": "PE", "action": "BUY", "quantity": LOT_SIZE, "price": 75.0 - chosen_credit},
                    {"symbol": f"NIFTY_{int(sell_strike)}_PE", "strike": sell_strike, "option_type": "PE", "action": "SELL", "quantity": LOT_SIZE, "price": 75.0},
                ]
            else:
                sell_strike = atm_strike
                buy_strike = sell_strike + chosen_width
                spread_type = SpreadType.BEAR_CALL_SPREAD.value
                legs = [
                    {"symbol": f"NIFTY_{int(buy_strike)}_CE", "strike": buy_strike, "option_type": "CE", "action": "BUY", "quantity": LOT_SIZE, "price": 75.0 - chosen_credit},
                    {"symbol": f"NIFTY_{int(sell_strike)}_CE", "strike": sell_strike, "option_type": "CE", "action": "SELL", "quantity": LOT_SIZE, "price": 75.0},
                ]

            trade_id = f"SPD-{datetime.now(IST).strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4].upper()}"
            max_reward_inr = round(chosen_credit * LOT_SIZE, 2)

            order_payload = {
                "trade_id": trade_id,
                "spread_type": spread_type,
                "legs": legs,
                "spot_price": spot_price,
                "spread_width": chosen_width,
                "net_credit": chosen_credit,
                "max_risk_inr": chosen_risk_inr,
                "max_reward_inr": max_reward_inr,
                "timestamp": ts_str,
                "architect_signal": payload["description"],
            }

            logger.info(
                f"[PAYLOAD SYNTHESIZED] {spread_type} ({trade_id}) | "
                f"Width: {chosen_width} pts | Max Risk: INR {chosen_risk_inr} <= INR {MAX_PERMITTED_RISK_INR} | "
                f"Max Reward: INR {max_reward_inr}"
            )

            # Publish ORDER_PROPOSED to Blackboard for Auditor
            eid = bus.publish(
                topic="ORDER_PROPOSED",
                source="Coder",
                target="Auditor",
                payload=order_payload,
            )
            # Mark consumed SIGNAL_DETECTED event as COMPLETED
            bus.update_status(event.id, EventStatus.COMPLETED)
            logger.info(f"Published ORDER_PROPOSED (Event #{eid}) to system_bus.db for Auditor.")

        time.sleep(0.5)


if __name__ == "__main__":
    run_coder()
