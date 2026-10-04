"""
Process 1: Architect Agent Worker
Blackboard Pattern: Uses bus.py (system_bus.db)
Responsibilities:
1. Tracks 5-min candles and calculates Initial Balance (09:15 - 09:45 AM IST).
2. Pure price action compression & chop filter (zero lagging indicators).
3. Identifies Support & Resistance zones.
4. Detects 5-min rejection candles (wick ratio >= 50%).
5. Publishes SIGNAL_DETECTED to Blackboard (system_bus.db) for Coder Agent.
"""

from __future__ import annotations

import logging
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

# Fix UTF-8 stdout on Windows
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from bus import SystemBus
from execution_engine import RejectionDetector, SignalType
from regime_filter import Candle, InitialBalance, MarketRegime, RegimeFilter

IST = ZoneInfo("Asia/Kolkata")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [ARCHITECT] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("ArchitectWorker")


def run_architect():
    bus = SystemBus()
    regime_filter = RegimeFilter(tz=IST)
    rejection_detector = RejectionDetector()
    recent_candles: list[Candle] = []

    print("\n" + "=" * 65)
    print(" [PROCESS 1] ARCHITECT AGENT - STRATEGY & REGIME INVARIANTS")
    print("  Pattern: Event-Driven Blackboard (system_bus.db)")
    print("  Output Topic: SIGNAL_DETECTED -> Target: Coder")
    print("=" * 65 + "\n")

    sim_time = datetime(2026, 10, 5, 9, 15, tzinfo=IST)

    # 1. Simulate 09:15 - 09:40 Initial Balance candles
    logger.info("Starting Initial Balance formation (09:15 - 09:45 AM IST)...")
    ib_candles = [
        Candle(sim_time + timedelta(minutes=0), 25000, 25060, 24980, 25040),
        Candle(sim_time + timedelta(minutes=5), 25040, 25080, 25020, 25070),
        Candle(sim_time + timedelta(minutes=10), 25070, 25075, 25010, 25015),
        Candle(sim_time + timedelta(minutes=15), 25015, 25030, 24950, 24960), # Low: 24950
        Candle(sim_time + timedelta(minutes=20), 24960, 25000, 24955, 24995),
        Candle(sim_time + timedelta(minutes=25), 24995, 25080, 24980, 25020), # High: 25080
    ]

    for c in ib_candles:
        recent_candles.append(c)
        regime_filter.ingest_ib_candle(c)
        logger.info(
            f"[IB CANDLE] {c.timestamp.strftime('%H:%M')} | O:{c.open} H:{c.high} L:{c.low} C:{c.close}"
        )
        time.sleep(0.4)

    regime_filter.ib_tracker.lock_manual()
    ib = regime_filter.ib_tracker.get_ib()
    logger.info(
        f"INITIAL BALANCE LOCKED: High=INR {ib.high:.1f} | Low=INR {ib.low:.1f} | Range={ib.range:.1f} pts"
    )

    # 2. Main Live Cycle: generate incoming 5-min candles and evaluate structural setups
    trade_candles = [
        # Candle 1 (10:15 IST): Bullish Hammer testing 24950 Support
        Candle(
            timestamp=datetime(2026, 10, 5, 10, 15, tzinfo=IST),
            open=24985.0,
            high=24990.0,
            low=24945.0,
            close=24985.0,
            volume=5000.0,
        ),
        # Candle 2 (11:30 IST): Second Bullish Hammer testing 24950 Support
        Candle(
            timestamp=datetime(2026, 10, 5, 11, 30, tzinfo=IST),
            open=24980.0,
            high=24985.0,
            low=24945.0,
            close=24980.0,
            volume=4500.0,
        ),
        # Candle 3 (13:00 IST): Third hammer (will trigger Auditor Veto on max 2 trades limit)
        Candle(
            timestamp=datetime(2026, 10, 5, 13, 0, tzinfo=IST),
            open=24982.0,
            high=24985.0,
            low=24945.0,
            close=24982.0,
            volume=4000.0,
        ),
    ]

    candle_idx = 0
    while True:
        if candle_idx < len(trade_candles):
            c = trade_candles[candle_idx]
            candle_idx += 1
            recent_candles.append(c)

            logger.info("-" * 50)
            logger.info(
                f"[5-MIN CANDLE INGESTED] {c.timestamp.strftime('%H:%M')} | "
                f"Close: {c.close:.1f} | Range: {c.range:.1f} pts"
            )

            # Evaluate regime
            regime = regime_filter.evaluate_regime(recent_candles, current_time=c.timestamp)
            logger.info(f"[REGIME] {regime.regime.value}: {regime.description}")

            if regime.is_favorable_for_spread:
                zones = rejection_detector.identify_sr_zones(ib=ib, spot_price=c.close)
                rejection = rejection_detector.detect_rejection(c, zones)

                if rejection.signal_type != SignalType.NONE:
                    logger.info(
                        f"[EDGE CONFIRMED] {rejection.description} (Confidence: {rejection.confidence})"
                    )

                    # Publish SIGNAL_DETECTED to Blackboard (system_bus.db)
                    payload = {
                        "signal_type": rejection.signal_type.value,
                        "spot_price": c.close,
                        "zone_name": rejection.zone.name,
                        "zone_level": rejection.zone.level,
                        "wick_ratio": rejection.wick_ratio,
                        "confidence": rejection.confidence,
                        "description": rejection.description,
                        "timestamp": c.timestamp.isoformat(),
                    }
                    eid = bus.publish(
                        topic="SIGNAL_DETECTED",
                        source="Architect",
                        target="Coder",
                        payload=payload,
                    )
                    logger.info(f"Published SIGNAL_DETECTED (Event ID #{eid}) to system_bus.db for Coder.")

        time.sleep(3.0)


if __name__ == "__main__":
    run_architect()
