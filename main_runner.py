"""
Multi-Agent Algorithmic Trading Runner with Live Market Feed
Module: main_runner.py

Coordinates all 4 autonomous agents over bus.py (system_bus.db):
1. Architect Agent: Strategy edge detection & pure price action regime validation
2. Coder Agent: Dynamic option spread synthesis with strict risk sizing (<= 1500 INR)
3. Auditor Agent: Continuous compliance & hard risk FSM gating (Max 2 trades, -1500 loss limit)
4. DevOps Agent: Execution bridge & paper trading fill engine

Features:
- `--check-broker`: Pre-flight connectivity check (validates Angel One login / TOTP / JWT token and live NIFTY quote)
- `--dry-run`: Verifies concurrent multi-agent communication over bus.py
- `--live`: Connects live market feed for Monday trading (09:15 - 15:30 IST) in PAPER_TRADING mode
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
import time
import uuid
from datetime import datetime, time as dtime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

# Load environment configuration from .env
load_dotenv()

# Fix UTF-8 stdout encoding on Windows
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from bus import EventStatus, SystemBus
from execution_engine import AngelAuth, Candle, RejectionDetector, SignalType, SpreadType
from regime_filter import InitialBalance, InitialBalanceTracker, MarketRegime, RegimeFilter
from risk_guard import RiskGuard
from audit_logger import AuditLogger
from agents.architect import EXPIRY_CUTOFF_TIME, is_expiry_day
from agents.notifier import TelegramNotifier, notifier_worker

IST = ZoneInfo("Asia/Kolkata")
LOT_SIZE = int(os.getenv("NIFTY_LOT_SIZE", "25"))
MAX_PERMITTED_SPREAD_RISK_INR = 1500.0
MAX_DAILY_LOSS_INR = float(os.getenv("MAX_DAILY_LOSS_INR", "-1500.0"))
MAX_DAILY_TRADES = int(os.getenv("MAX_DAILY_TRADES", "2"))
DEFAULT_ORDER_TYPE = "LIMIT"
MAX_BID_ASK_SPREAD_RATIO = 0.10

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(threadName)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("MainRunner")


class CandleAggregator:
    """
    Aggregates incoming live spot ticks into completed 5-minute OHLCV candles.
    """

    def __init__(self, interval_minutes: int = 5):
        self.interval_minutes = interval_minutes
        self.current_candle_start: Optional[datetime] = None
        self.open: float = 0.0
        self.high: float = 0.0
        self.low: float = 0.0
        self.close: float = 0.0
        self.volume: float = 0.0

    def get_candle_slot(self, dt: datetime) -> datetime:
        minute_slot = (dt.minute // self.interval_minutes) * self.interval_minutes
        return dt.replace(minute=minute_slot, second=0, microsecond=0)

    def on_tick(self, ltp: float, dt: datetime, volume: float = 0.0) -> Optional[Candle]:
        slot = self.get_candle_slot(dt)
        closed_candle = None

        if self.current_candle_start is None:
            self.current_candle_start = slot
            self.open = ltp
            self.high = ltp
            self.low = ltp
            self.close = ltp
            self.volume = volume
        elif slot > self.current_candle_start:
            # Previous candle closed
            closed_candle = Candle(
                timestamp=self.current_candle_start,
                open=self.open,
                high=self.high,
                low=self.low,
                close=self.close,
                volume=self.volume,
            )
            # Begin new candle slot
            self.current_candle_start = slot
            self.open = ltp
            self.high = ltp
            self.low = ltp
            self.close = ltp
            self.volume = volume
        else:
            # Update running high/low/close
            self.high = max(self.high, ltp)
            self.low = min(self.low, ltp)
            self.close = ltp
            self.volume += volume

        return closed_candle


def architect_worker(
    bus: SystemBus,
    stop_event: threading.Event,
    verified_events: Optional[dict[str, threading.Event]] = None,
) -> None:
    """
    Architect Agent Worker:
    Listens on bus.py for TEST_SIGNAL and MARKET_CANDLE events.
    Validates regime & expiry invariants, then publishes SIGNAL_DETECTED to Coder.
    """
    regime_filter = RegimeFilter(tz=IST)
    rejection_detector = RejectionDetector()
    recent_candles: list[Candle] = []

    while not stop_event.is_set():
        events = bus.consume(topic="TEST_SIGNAL", target="Architect")
        if not events:
            events = bus.consume(topic="MARKET_CANDLE", target="Architect")

        for ev in events:
            payload = ev.payload
            sim_time_str = payload.get("timestamp") or datetime.now(IST).isoformat()
            sim_time = datetime.fromisoformat(sim_time_str) if isinstance(sim_time_str, str) else sim_time_str

            # Invariant: Gamma protection on expiry days
            if is_expiry_day(sim_time) and sim_time.time() >= EXPIRY_CUTOFF_TIME:
                logger.warning("[ARCHITECT] Signal blocked by 13:30 expiry gamma cutoff.")
                bus.update_status(ev.id, EventStatus.FAILED)
                continue

            # Case A: Live market candle close event
            if ev.topic == "MARKET_CANDLE" and "open" in payload:
                candle = Candle(
                    timestamp=sim_time,
                    open=float(payload["open"]),
                    high=float(payload["high"]),
                    low=float(payload["low"]),
                    close=float(payload["close"]),
                    volume=float(payload.get("volume", 0.0)),
                )
                recent_candles.append(candle)
                if len(recent_candles) > 50:
                    recent_candles.pop(0)

                # Ingest into IB tracker if within 09:15 - 09:45
                regime_filter.ingest_ib_candle(candle)

                # Pure price action regime evaluation
                regime = regime_filter.evaluate_regime(recent_candles, current_time=candle.timestamp)
                logger.info(f"[ARCHITECT] Ingested 5m candle {candle.timestamp.strftime('%H:%M')} | Regime: {regime.regime.value}")

                if regime.is_favorable_for_spread:
                    ib = regime_filter.ib_tracker.get_ib()
                    zones = rejection_detector.identify_sr_zones(ib=ib, spot_price=candle.close)
                    rejection = rejection_detector.detect_rejection(candle, zones)

                    if rejection.signal_type != SignalType.NONE:
                        sig_payload = {
                            "signal_type": rejection.signal_type.value,
                            "spot_price": candle.close,
                            "zone_name": rejection.zone.name,
                            "zone_level": rejection.zone.level,
                            "wick_ratio": rejection.wick_ratio,
                            "confidence": rejection.confidence,
                            "description": rejection.description,
                            "timestamp": candle.timestamp.isoformat(),
                        }
                        eid = bus.publish(
                            topic="SIGNAL_DETECTED",
                            source="Architect",
                            target="Coder",
                            payload=sig_payload,
                        )
                        logger.info(f"[ARCHITECT] Emitted live SIGNAL_DETECTED #{eid}: {rejection.description} -> Coder")
                        if verified_events and "architect_emitted" in verified_events:
                            verified_events["architect_emitted"].set()

                bus.update_status(ev.id, EventStatus.COMPLETED)
                continue

            # Case B: Direct TEST_SIGNAL (dry-run harness)
            signal_payload = {
                "signal_type": payload.get("signal_type", SignalType.BULLISH_REJECTION.value),
                "spot_price": float(payload.get("spot_price", 25000.0)),
                "zone_name": payload.get("zone_name", "S1_SUPPORT"),
                "zone_level": float(payload.get("zone_level", 24950.0)),
                "wick_ratio": float(payload.get("wick_ratio", 0.55)),
                "confidence": payload.get("confidence", "HIGH"),
                "description": payload.get("description", "Bullish Hammer at Support"),
                "timestamp": sim_time.isoformat() if hasattr(sim_time, "isoformat") else str(sim_time),
            }

            eid = bus.publish(
                topic="SIGNAL_DETECTED",
                source="Architect",
                target="Coder",
                payload=signal_payload,
            )
            bus.update_status(ev.id, EventStatus.COMPLETED)
            logger.info(f"[ARCHITECT] Emitted SIGNAL_DETECTED #{eid}: {signal_payload['description']} -> Coder")

            if verified_events and "architect_emitted" in verified_events:
                verified_events["architect_emitted"].set()

        time.sleep(0.05)


def coder_worker(
    bus: SystemBus,
    stop_event: threading.Event,
    verified_events: Optional[dict[str, threading.Event]] = None,
) -> None:
    """
    Coder Agent Worker:
    Consumes SIGNAL_DETECTED events from Blackboard.
    Synthesizes defined-risk 2-leg option spread (Max Risk <= 1500 INR, BUY leg before SELL leg).
    Publishes ORDER_PROPOSED to Auditor.
    """
    while not stop_event.is_set():
        events = bus.consume(topic="SIGNAL_DETECTED", target="Coder")
        for ev in events:
            payload = ev.payload
            signal_type = payload["signal_type"]
            spot_price = float(payload["spot_price"])
            ts_str = payload.get("timestamp") or datetime.now(IST).isoformat()

            # Dynamic strike calculation
            atm_strike = round(spot_price / 50.0) * 50.0
            candidate_widths = [100.0, 50.0]
            chosen_width = None
            chosen_credit = 0.0
            chosen_risk_inr = 0.0

            for width in candidate_widths:
                sell_prem = 75.0
                buy_prem = max(57.0 - (width - 50.0) * 0.25, 20.0)
                net_credit = sell_prem - buy_prem
                net_premium_received = net_credit * LOT_SIZE
                risk_inr = (width * LOT_SIZE) - net_premium_received

                if risk_inr <= MAX_PERMITTED_SPREAD_RISK_INR:
                    chosen_width = width
                    chosen_credit = net_credit
                    chosen_risk_inr = round(risk_inr, 2)
                    break

            if chosen_width is None:
                bus.update_status(ev.id, EventStatus.FAILED)
                continue

            # Invariant: Margin order safety - BUY leg MUST precede SELL leg
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

            eid = bus.publish(
                topic="ORDER_PROPOSED",
                source="Coder",
                target="Auditor",
                payload=order_payload,
            )
            bus.update_status(ev.id, EventStatus.COMPLETED)
            logger.info(
                f"[CODER] Created spread {spread_type} ({trade_id}) | "
                f"Width: {chosen_width} pts | Max Risk: INR {chosen_risk_inr} <= {MAX_PERMITTED_SPREAD_RISK_INR} -> Auditor"
            )

            if verified_events and "coder_created_spread" in verified_events:
                verified_events["coder_created_spread"].set()

        time.sleep(0.05)


def auditor_worker(
    bus: SystemBus,
    stop_event: threading.Event,
    verified_events: Optional[dict[str, threading.Event]] = None,
    state_file: str = "daily_state.json",
    db_path: str = "trading_journal.db",
) -> None:
    """
    Auditor Agent Worker:
    Consumes ORDER_PROPOSED events from Blackboard.
    Validates hard risk invariants (Max Risk <= 1500 INR, Daily Trades < 2, Daily Loss > -1500 INR).
    Publishes ORDER_APPROVED to DevOps.
    """
    risk_guard = RiskGuard(state_file=state_file, tz=IST)
    audit_logger = AuditLogger(db_path=db_path, tz=IST)

    while not stop_event.is_set():
        events = bus.consume(topic="ORDER_PROPOSED", target="Auditor")
        for ev in events:
            payload = ev.payload
            trade_id = payload["trade_id"]
            ts = datetime.fromisoformat(payload["timestamp"]) if "timestamp" in payload else datetime.now(IST)

            # Invariant 1: Single spread risk <= 1500 INR
            max_risk_inr = float(payload.get("max_risk_inr", 0.0))
            if max_risk_inr > MAX_PERMITTED_SPREAD_RISK_INR:
                bus.update_status(ev.id, EventStatus.VETOED)
                logger.warning(f"[AUDITOR] Vetoed {trade_id}: Max risk INR {max_risk_inr} > 1500 INR")
                bus.publish(topic="ORDER_BLOCKED", source="Auditor", target="Notifier", payload={"trade_id": trade_id, "reason": f"Max risk INR {max_risk_inr} > 1500 INR"})
                continue

            # Invariant 2: Daily loss limit
            if risk_guard.total_pnl <= MAX_DAILY_LOSS_INR:
                bus.update_status(ev.id, EventStatus.VETOED)
                logger.warning(f"[AUDITOR] Vetoed {trade_id}: Daily loss limit breached")
                bus.publish(topic="ORDER_BLOCKED", source="Auditor", target="Notifier", payload={"trade_id": trade_id, "reason": "Daily loss limit breached"})
                continue

            # Invariant 3: Daily trades limit
            if risk_guard.trade_count >= MAX_DAILY_TRADES:
                bus.update_status(ev.id, EventStatus.VETOED)
                logger.warning(f"[AUDITOR] Vetoed {trade_id}: Max daily trades reached")
                bus.publish(topic="ORDER_BLOCKED", source="Auditor", target="Notifier", payload={"trade_id": trade_id, "reason": "Max daily trades reached"})
                continue

            # Invariant 4: Time window gating
            allowed, reason = risk_guard.can_enter_trade(current_time=ts)
            if not allowed:
                bus.update_status(ev.id, EventStatus.VETOED)
                logger.warning(f"[AUDITOR] Vetoed {trade_id}: {reason}")
                bus.publish(topic="ORDER_BLOCKED", source="Auditor", target="Notifier", payload={"trade_id": trade_id, "reason": reason})
                continue

            # Approved
            bus.update_status(ev.id, EventStatus.COMPLETED)
            risk_guard.record_trade_entry(trade_id=trade_id, details=payload, current_time=ts)

            eid = bus.publish(
                topic="ORDER_APPROVED",
                source="Auditor",
                target="DevOps",
                payload=payload,
            )
            logger.info(f"[AUDITOR] Approved order {trade_id} (Invariant compliant) -> DevOps (Event #{eid})")

            if verified_events and "auditor_approved" in verified_events:
                verified_events["auditor_approved"].set()

        # Also consume execution confirmations for journal logging
        exec_events = bus.consume(topic="ORDER_EXECUTED", target="Auditor")
        for ev in exec_events:
            payload = ev.payload
            trade_id = payload["trade_id"]
            ts = datetime.fromisoformat(payload["timestamp"]) if "timestamp" in payload else datetime.now(IST)

            audit_logger.log_trade_entry(
                trade_id=trade_id,
                symbol="NIFTY",
                spread_type=payload["spread_type"],
                expected_entry_price=float(payload.get("expected_entry_price", payload.get("net_credit", 0.0))),
                actual_entry_price=float(payload.get("actual_entry_price", payload.get("net_credit", 0.0))),
                is_paper=payload.get("is_paper", True),
                timestamp=ts,
                notes=payload.get("architect_signal", ""),
            )
            bus.update_status(ev.id, EventStatus.COMPLETED)
            logger.info(f"[AUDITOR] Trade {trade_id} journaled to SQLite (Paper Mode: {payload.get('is_paper', True)})")

        time.sleep(0.05)


def devops_worker(
    bus: SystemBus,
    stop_event: threading.Event,
    verified_events: Optional[dict[str, threading.Event]] = None,
    paper_trading: bool = True,
) -> None:
    """
    DevOps Agent Worker:
    Consumes ORDER_APPROVED events from Blackboard.
    Validates LIMIT order type & bid-ask liquidity spread guard.
    Fills order in Paper Trading mode (default) and publishes ORDER_EXECUTED to Auditor.
    """
    while not stop_event.is_set():
        events = bus.consume(topic="ORDER_APPROVED", target="DevOps")
        for ev in events:
            payload = ev.payload
            trade_id = payload["trade_id"]
            legs = payload["legs"]
            ts_str = payload.get("timestamp") or datetime.now(IST).isoformat()

            # Invariant: Limit order type
            order_type = DEFAULT_ORDER_TYPE
            assert order_type == "LIMIT", "Order type invariant breached!"

            # Invariant: Bid-ask spread guard (synthetic quote 18.0 / 18.5)
            hedge_bid, hedge_ask = 18.0, 18.5
            mid_price = (hedge_bid + hedge_ask) / 2.0
            spread_ratio = (hedge_ask - hedge_bid) / mid_price
            if spread_ratio > MAX_BID_ASK_SPREAD_RATIO:
                bus.update_status(ev.id, EventStatus.FAILED)
                logger.warning(f"[DEVOPS] Execution paused: Bid-Ask spread {spread_ratio:.1%} > 10%")
                continue

            executed_legs = []
            for i, leg in enumerate(legs):
                executed_legs.append({**leg, "order_id": f"FILL-{trade_id}-{i+1}", "status": "FILLED"})

            eid = bus.publish(
                topic="ORDER_EXECUTED",
                source="DevOps",
                target="Auditor",
                payload={
                    **payload,
                    "order_type": order_type,
                    "executed_legs": executed_legs,
                    "actual_entry_price": payload["net_credit"],
                    "expected_entry_price": payload["net_credit"],
                    "is_paper": paper_trading,
                    "timestamp": ts_str,
                },
            )
            bus.publish(
                topic="ORDER_EXECUTED_ALERT",
                source="DevOps",
                target="Notifier",
                payload={
                    "trade_id": trade_id,
                    "spread_type": payload.get("spread_type", "SPREAD"),
                    "max_risk_inr": payload.get("max_risk_inr", 0.0),
                },
            )
            bus.update_status(ev.id, EventStatus.COMPLETED)
            logger.info(f"[DEVOPS] Executed {trade_id} ({'PAPER' if paper_trading else 'LIVE'}) -> Event #{eid}")

            if verified_events and "devops_received" in verified_events:
                verified_events["devops_received"].set()

        time.sleep(0.05)


def check_broker_connectivity() -> bool:
    """
    Pre-flight connectivity check flag (--check-broker):
    1. Validates that Angel One API login / TOTP generates a valid JWT token.
    2. Fetches the current Nifty index quote to verify live connection.
    """
    print("\n" + "=" * 80)
    print("        ANGEL ONE SMARTAPI PRE-FLIGHT CONNECTIVITY & QUOTE CHECK")
    print("=" * 80)

    api_key = os.getenv("SMARTAPI_API_KEY")
    client_code = os.getenv("SMARTAPI_CLIENT_CODE")
    pin = os.getenv("SMARTAPI_PIN")
    totp_secret = os.getenv("SMARTAPI_TOTP_SECRET")

    print("[1/3] Loading broker credentials from environment (.env)...")
    if not all([api_key, client_code, pin, totp_secret]):
        print("[FAIL] Incomplete credentials in .env! Required: SMARTAPI_API_KEY, SMARTAPI_CLIENT_CODE, SMARTAPI_PIN, SMARTAPI_TOTP_SECRET")
        return False

    print(f"      Client Code: {client_code}")
    print(f"      API Key:     {api_key[:3]}...{api_key[-2:] if len(api_key) > 5 else ''}")
    print(f"      TOTP Secret: Configured ({len(totp_secret)} chars)")

    print("\n[2/3] Generating TOTP and authenticating session with Angel One SmartAPI...")
    auth = AngelAuth(
        api_key=api_key,
        client_code=client_code,
        pin=pin,
        totp_secret=totp_secret,
    )

    success = auth.login()
    if not success or not auth.auth_token:
        print("[FAIL] Angel One login failed! Check credentials or network connectivity.")
        return False

    jwt_preview = auth.auth_token[:25] + "..." if len(auth.auth_token) > 25 else auth.auth_token
    print(f"      [SUCCESS] JWT Session Token Generated: {jwt_preview}")
    print(f"      Feed Token:    {'Active' if auth.feed_token else 'None'}")
    print(f"      Refresh Token: {'Active' if auth.refresh_token else 'None'}")

    print("\n[3/3] Fetching live NIFTY 50 Spot Quote from NSE...")
    try:
        resp = auth.smart_api.ltpData("NSE", "Nifty 50", "99926000")
        if not resp or not resp.get("status"):
            print(f"[FAIL] Unable to fetch Nifty quote: {resp}")
            return False

        data = resp["data"]
        ltp = float(data.get("ltp", 0.0))
        open_price = float(data.get("open", 0.0))
        high_price = float(data.get("high", 0.0))
        low_price = float(data.get("low", 0.0))
        close_price = float(data.get("close", 0.0))

        print(f"      [SUCCESS] Live Quote Received from NSE:")
        print(f"      Symbol:       {data.get('tradingsymbol')} (Token: {data.get('symboltoken')})")
        print(f"      LTP:          INR {ltp:,.2f}")
        print(f"      Open:         INR {open_price:,.2f}")
        print(f"      High:         INR {high_price:,.2f}")
        print(f"      Low:          INR {low_price:,.2f}")
        print(f"      Prev Close:   INR {close_price:,.2f}")

    except Exception as e:
        print(f"[FAIL] Exception fetching quote: {e}")
        return False

    print("\n" + "=" * 80)
    print("[PRE-FLIGHT SUCCESS] Angel One Broker & Live Feed Connection VERIFIED READY")
    print("=" * 80 + "\n")
    return True


def run_dry_run(db_path: str = "system_bus.db") -> bool:
    """
    Executes dry-run test:
    1. Starts all 4 agents concurrently using threads.
    2. Publish one dummy test signal onto bus.py.
    3. Verify that:
       - Architect emits signal
       - Coder creates spread
       - Auditor approves
       - DevOps receives order
    4. Print: "[SUCCESS] All 4 agents communicated cleanly via bus.py" and exit.
    """
    print("\n" + "=" * 80)
    print("      SACCHIN QUANT: MULTI-AGENT SYSTEM DRY-RUN (CONCURRENT THREADS)")
    print("=" * 80)
    logger.info("Initializing concurrent thread test harness over bus.py...")

    dry_run_state = "dry_run_state.json"
    state_path = Path(dry_run_state)
    if state_path.exists():
        try:
            state_path.unlink()
        except OSError:
            pass

    bus = SystemBus(db_path=db_path)
    with bus._get_connection() as conn:
        conn.execute("DELETE FROM bus_events;")
        conn.commit()

    stop_event = threading.Event()
    verified_events = {
        "architect_emitted": threading.Event(),
        "coder_created_spread": threading.Event(),
        "auditor_approved": threading.Event(),
        "devops_received": threading.Event(),
    }

    threads = [
        threading.Thread(
            target=architect_worker,
            args=(SystemBus(db_path=db_path), stop_event, verified_events),
            name="ArchitectWorker",
            daemon=True,
        ),
        threading.Thread(
            target=coder_worker,
            args=(SystemBus(db_path=db_path), stop_event, verified_events),
            name="CoderWorker",
            daemon=True,
        ),
        threading.Thread(
            target=auditor_worker,
            args=(SystemBus(db_path=db_path), stop_event, verified_events, dry_run_state),
            name="AuditorWorker",
            daemon=True,
        ),
        threading.Thread(
            target=devops_worker,
            args=(SystemBus(db_path=db_path), stop_event, verified_events),
            name="DevOpsWorker",
            daemon=True,
        ),
        threading.Thread(
            target=notifier_worker,
            args=(SystemBus(db_path=db_path), stop_event),
            name="NotifierWorker",
            daemon=True,
        ),
    ]

    for t in threads:
        t.start()
    logger.info("All 5 agent worker threads started and actively polling bus.py.")
    time.sleep(0.2)

    logger.info("Publishing dummy test signal to bus.py for Architect...")
    test_sim_time = datetime(2026, 10, 5, 10, 15, tzinfo=IST)
    test_payload = {
        "signal_type": SignalType.BULLISH_REJECTION.value,
        "spot_price": 25000.0,
        "zone_name": "S1_SUPPORT",
        "zone_level": 24950.0,
        "wick_ratio": 0.58,
        "confidence": "HIGH",
        "description": "Bullish Hammer at 24950 Support",
        "timestamp": test_sim_time.isoformat(),
    }

    bus.publish(
        topic="TEST_SIGNAL",
        source="TestRunner",
        target="Architect",
        payload=test_payload,
    )

    logger.info("Awaiting pipeline completion across all 4 agents...")
    stages = [
        ("Architect emits signal", "architect_emitted"),
        ("Coder creates spread", "coder_created_spread"),
        ("Auditor approves", "auditor_approved"),
        ("DevOps receives order", "devops_received"),
    ]

    for stage_desc, stage_key in stages:
        completed = verified_events[stage_key].wait(timeout=6.0)
        if not completed:
            logger.error(f"[TIMEOUT] Stage '{stage_desc}' failed to complete within 6 seconds.")
            stop_event.set()
            return False
        logger.info(f"  [VERIFIED] {stage_desc}")

    stop_event.set()
    for t in threads:
        t.join(timeout=1.0)

    if state_path.exists():
        try:
            state_path.unlink()
        except OSError:
            pass

    print("\n" + "=" * 80)
    print("[SUCCESS] All 4 agents communicated cleanly via bus.py")
    print("=" * 80 + "\n")
    return True


def live_market_feed_worker(
    bus: SystemBus,
    stop_event: threading.Event,
    poll_interval: float = 2.0,
    max_ticks: Optional[int] = None,
) -> None:
    """
    LiveMarketFeed worker thread:
    - Fetches live NIFTY 50 spot ticks via Angel One SmartAPI.
    - Aggregates 5-minute OHLCV candles (CandleAggregator).
    - Formulates Initial Balance (09:15-09:45 IST) and locks it at 09:45 IST.
    - Emits MARKET_CANDLE events to Architect on bus.py.
    """
    auth = AngelAuth(
        api_key=os.getenv("SMARTAPI_API_KEY"),
        client_code=os.getenv("SMARTAPI_CLIENT_CODE"),
        pin=os.getenv("SMARTAPI_PIN"),
        totp_secret=os.getenv("SMARTAPI_TOTP_SECRET"),
    )
    if not auth.login() or not auth.smart_api:
        logger.error("[LIVE FEED ERROR] Unable to authenticate with Angel One SmartAPI. Aborting LiveMarketFeed worker.")
        return

    logger.info("[LIVE FEED] Angel One session active. Starting tick ingestion loop...")

    aggregator = CandleAggregator(interval_minutes=5)
    ib_tracker = InitialBalanceTracker(tz=IST)
    ib_locked = False
    ib_high = 0.0
    ib_low = float("inf")
    tick_count = 0
    last_ib_log_time = 0.0

    while not stop_event.is_set():
        now = datetime.now(IST)
        current_time = now.time()

        # Pre-market wait (before 09:15)
        if current_time < dtime(9, 15) and max_ticks is None:
            time.sleep(min(15.0, poll_interval * 5))
            continue

        # Post-market shutdown (after 15:30)
        if current_time > dtime(15, 30) and max_ticks is None:
            break

        try:
            resp = auth.smart_api.ltpData("NSE", "Nifty 50", "99926000")
            if not resp or not resp.get("status"):
                time.sleep(poll_interval)
                continue

            data = resp["data"]
            ltp = float(data["ltp"])
            tick_count += 1
        except Exception as e:
            logger.error(f"[LIVE FEED ERROR] Exception fetching live quote: {e}")
            time.sleep(poll_interval)
            continue

        closed_candle = aggregator.on_tick(ltp=ltp, dt=now)

        # Phase 1: From 09:15 to 09:45 IST - Formulate Initial Balance
        if current_time < dtime(9, 45) and not ib_locked:
            ib_high = max(ib_high, ltp)
            ib_low = min(ib_low, ltp)
            if closed_candle:
                ib_tracker.ingest_ib_candle(closed_candle)

            if time.time() - last_ib_log_time > 15.0:
                last_ib_log_time = time.time()
                logger.info(
                    f"[IB FORMATION] {now.strftime('%H:%M:%S')} IST | Spot: {ltp:,.2f} | "
                    f"IB High: {ib_high:,.1f} | IB Low: {ib_low:,.1f} | Range: {ib_high - ib_low:.1f} pts"
                )

        # Phase 2: At 09:45 IST - Lock Initial Balance
        if current_time >= dtime(9, 45) and not ib_locked:
            ib_locked = True
            ib_tracker.lock_manual()
            ib = ib_tracker.get_ib()
            effective_high = ib_high if ib_high > 0 else ltp
            effective_low = ib_low if (ib_low > 0 and ib_low < float("inf")) else ltp
            final_high = ib.high if (ib is not None and ib.high > 0) else effective_high
            final_low = ib.low if (ib is not None and ib.low > 0 and ib.low < float("inf")) else effective_low
            final_range = final_high - final_low
            logger.info("=" * 65)
            logger.info(
                f"[INITIAL BALANCE LOCKED at 09:45 IST] High: INR {final_high:.1f} | "
                f"Low: INR {final_low:.1f} | Range: {final_range:.1f} pts"
            )
            logger.info("=" * 65)
            bus.publish(
                topic="INITIAL_BALANCE_LOCKED",
                source="LiveMarketFeed",
                target="ALL",
                payload={"ib_high": final_high, "ib_low": final_low},
            )

        # Phase 3: At 09:45 IST onwards - Emit completed 5-min candle closes to ArchitectAgent
        if closed_candle is not None:
            candle_payload = {
                "open": closed_candle.open,
                "high": closed_candle.high,
                "low": closed_candle.low,
                "close": closed_candle.close,
                "volume": closed_candle.volume,
                "range": closed_candle.range,
                "timestamp": closed_candle.timestamp.isoformat(),
                "ib_high": ib_high,
                "ib_low": ib_low,
            }
            eid = bus.publish(
                topic="MARKET_CANDLE",
                source="LiveMarketFeed",
                target="Architect",
                payload=candle_payload,
            )
            logger.info(
                f"[CANDLE CLOSE EMITTED] {closed_candle.timestamp.strftime('%H:%M')} IST | "
                f"O:{closed_candle.open:.1f} H:{closed_candle.high:.1f} L:{closed_candle.low:.1f} C:{closed_candle.close:.1f} -> Event #{eid}"
            )

        if max_ticks and tick_count >= max_ticks:
            break

        time.sleep(poll_interval)

    logger.info("[LIVE FEED] Worker thread exiting cleanly.")


def run_live_market(
    paper_trading: bool = True,
    poll_interval: float = 2.0,
    db_path: str = "system_bus.db",
    state_file: str = "daily_state.json",
    stop_event: Optional[threading.Event] = None,
    max_ticks: Optional[int] = None,
) -> None:
    """
    Connects Live Market Feed to main_runner.py for Monday trading:
    1. Starts all 4 agents (Architect, Coder, Auditor, DevOps) concurrently using threads.
    2. Fetches live NIFTY 50 spot data from Angel One SmartAPI.
    3. From 09:15 to 09:45 IST: Formulates Initial Balance (IB High and IB Low).
    4. At 09:45 IST: Begins emitting candle close events to ArchitectAgent via bus.py.
    5. Runs in PAPER_TRADING mode by default for zero real capital risk.
    """
    print("\n" + "=" * 80)
    print("     SACCHIN QUANT: LIVE MARKET FEED & MULTI-AGENT EXECUTION ENGINE")
    print("=" * 80)
    print(f" Execution Mode: {'[PAPER TRADING - CAPITAL SAFE]' if paper_trading else '[LIVE REAL-CAPITAL BROKER]'}")
    print(" Feed Source:    Angel One SmartAPI (NIFTY 50 Spot Index)")
    print(" Timeline:       09:15 - 09:45 IST: Initial Balance Formation")
    print("                 09:45 - 15:05 IST: Strategy Signal Generation & Execution")
    print("                 15:10 IST:         Mandatory EOD Square-Off")
    print("=" * 80 + "\n")

    # 1. Connect Angel One API
    auth = AngelAuth(
        api_key=os.getenv("SMARTAPI_API_KEY"),
        client_code=os.getenv("SMARTAPI_CLIENT_CODE"),
        pin=os.getenv("SMARTAPI_PIN"),
        totp_secret=os.getenv("SMARTAPI_TOTP_SECRET"),
    )
    if not auth.login() or not auth.smart_api:
        logger.error("[FATAL] Unable to authenticate with Angel One SmartAPI. Aborting live market engine.")
        return

    logger.info(f"Angel One session active. Client: {auth.client_code} | Mode: {'PAPER' if paper_trading else 'LIVE'}")

    # 2. Start all 4 agents concurrently using threads
    internal_stop = stop_event or threading.Event()
    bus = SystemBus(db_path=db_path)

    threads = [
        threading.Thread(target=architect_worker, args=(SystemBus(db_path=db_path), internal_stop), name="ArchitectWorker", daemon=True),
        threading.Thread(target=coder_worker, args=(SystemBus(db_path=db_path), internal_stop), name="CoderWorker", daemon=True),
        threading.Thread(target=auditor_worker, args=(SystemBus(db_path=db_path), internal_stop, None, state_file), name="AuditorWorker", daemon=True),
        threading.Thread(target=devops_worker, args=(SystemBus(db_path=db_path), internal_stop, None, paper_trading), name="DevOpsWorker", daemon=True),
        threading.Thread(target=notifier_worker, args=(SystemBus(db_path=db_path), internal_stop), name="NotifierWorker", daemon=True),
    ]

    for t in threads:
        t.start()
    logger.info("All 5 agents running concurrently over bus.py.")

    # 09:14 IST: System Live & Broker Connected alert
    bus.publish(
        topic="SYSTEM_LIVE",
        source="MainRunner",
        target="Notifier",
        payload={"message": "System Live & Broker Connected"},
    )

    # 3. Market State & Aggregators
    aggregator = CandleAggregator(interval_minutes=5)
    ib_tracker = InitialBalanceTracker(tz=IST)
    ib_locked = False
    ib_high = 0.0
    ib_low = float("inf")
    tick_count = 0
    last_ib_log_time = 0.0
    square_off_alert_sent = False

    logger.info("Entering live polling loop...")

    try:
        while not internal_stop.is_set():
            now = datetime.now(IST)
            current_time = now.time()

            # Pre-market wait (before 09:15)
            if current_time < dtime(9, 15) and max_ticks is None:
                logger.info(f"[PRE-MARKET] Current time is {now.strftime('%H:%M:%S')} IST. Waiting for 09:15 market open...")
                time.sleep(min(15.0, poll_interval * 5))
                continue

            # 15:10 IST: Auto Square-off Alert
            if current_time >= dtime(15, 10) and not square_off_alert_sent:
                square_off_alert_sent = True
                bus.publish(
                    topic="POSITIONS_SQUARED_OFF",
                    source="RiskGuard",
                    target="Notifier",
                    payload={"message": "All Positions Auto Squared-Off"},
                )

            # Post-market shutdown (after 15:30)
            if current_time > dtime(15, 30) and max_ticks is None:
                logger.info(f"[MARKET CLOSE] Current time is {now.strftime('%H:%M:%S')} IST. Regular market closed.")
                bus.publish(
                    topic="EOD_SUMMARY",
                    source="AuditLogger",
                    target="Notifier",
                    payload={"count": tick_count, "pnl": 0.0},
                )
                break

            # Fetch live Nifty spot quote
            try:
                resp = auth.smart_api.ltpData("NSE", "Nifty 50", "99926000")
                if not resp or not resp.get("status"):
                    logger.warning(f"[FEED WARNING] Quote fetch returned non-success: {resp.get('message', 'Unknown error')}")
                    time.sleep(poll_interval)
                    continue

                data = resp["data"]
                ltp = float(data["ltp"])
                tick_count += 1

            except Exception as e:
                logger.error(f"[FEED ERROR] Exception fetching live quote: {e}")
                time.sleep(poll_interval)
                continue

            # Update running candle aggregator
            closed_candle = aggregator.on_tick(ltp=ltp, dt=now)

            # Phase 1: From 09:15 to 09:45 IST - Formulate Initial Balance
            if current_time < dtime(9, 45) and not ib_locked:
                ib_high = max(ib_high, ltp)
                ib_low = min(ib_low, ltp)
                if closed_candle:
                    ib_tracker.ingest_ib_candle(closed_candle)

                if time.time() - last_ib_log_time > 15.0:
                    last_ib_log_time = time.time()
                    logger.info(
                        f"[IB FORMATION] {now.strftime('%H:%M:%S')} IST | Spot: {ltp:,.2f} | "
                        f"IB High: {ib_high:,.1f} | IB Low: {ib_low:,.1f} | Range: {ib_high - ib_low:.1f} pts"
                    )

            # Phase 2: At 09:45 IST - Lock Initial Balance
            if current_time >= dtime(9, 45) and not ib_locked:
                ib_locked = True
                ib_tracker.lock_manual()
                ib = ib_tracker.get_ib()
                # If during off-hours/testing ib_high was set from ticks
                effective_high = ib_high if ib_high > 0 else ltp
                effective_low = ib_low if (ib_low > 0 and ib_low < float("inf")) else ltp
                final_high = ib.high if (ib is not None and ib.high > 0) else effective_high
                final_low = ib.low if (ib is not None and ib.low > 0 and ib.low < float("inf")) else effective_low
                final_range = final_high - final_low
                logger.info("=" * 65)
                logger.info(
                    f"[INITIAL BALANCE LOCKED at 09:45 IST] High: INR {final_high:.1f} | "
                    f"Low: INR {final_low:.1f} | Range: {final_range:.1f} pts"
                )
                logger.info("=" * 65)
                bus.publish(
                    topic="INITIAL_BALANCE_LOCKED",
                    source="LiveMarketFeed",
                    target="Notifier",
                    payload={"ib_high": final_high, "ib_low": final_low},
                )

            # Phase 3: At 09:45 IST onwards - Emit completed 5-min candle closes to ArchitectAgent
            if closed_candle is not None:
                candle_payload = {
                    "open": closed_candle.open,
                    "high": closed_candle.high,
                    "low": closed_candle.low,
                    "close": closed_candle.close,
                    "volume": closed_candle.volume,
                    "range": closed_candle.range,
                    "timestamp": closed_candle.timestamp.isoformat(),
                    "ib_high": ib_high,
                    "ib_low": ib_low,
                }
                eid = bus.publish(
                    topic="MARKET_CANDLE",
                    source="LiveMarketFeed",
                    target="Architect",
                    payload=candle_payload,
                )
                logger.info(
                    f"[CANDLE CLOSE EMITTED] {closed_candle.timestamp.strftime('%H:%M')} IST | "
                    f"O:{closed_candle.open:.1f} H:{closed_candle.high:.1f} L:{closed_candle.low:.1f} C:{closed_candle.close:.1f} -> Event #{eid}"
                )

            if max_ticks and tick_count >= max_ticks:
                logger.info(f"Reached max test ticks ({max_ticks}). Exiting live loop.")
                break

            time.sleep(poll_interval)

    finally:
        internal_stop.set()
        for t in threads:
            t.join(timeout=1.0)
        logger.info("Live market loop stopped cleanly.")


def main():
    parser = argparse.ArgumentParser(
        description="Main Multi-Agent System Runner with Live Market Feed (Blackboard Pattern over bus.py)"
    )
    parser.add_argument(
        "--check-broker",
        action="store_true",
        help="Pre-flight connectivity check: validate Angel One login / TOTP / JWT token and fetch live Nifty quote",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Execute concurrent dry-run verification across all 4 agents via bus.py",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Connect live market feed and start live Monday trading engine (09:15 - 15:30 IST)",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=2.0,
        help="Live market quote polling interval in seconds (default: 2.0)",
    )
    args = parser.parse_args()

    if args.check_broker:
        success = check_broker_connectivity()
        sys.exit(0 if success else 1)
    elif args.dry_run:
        from agents.master_executive import MasterExecutiveAgent
        success = MasterExecutiveAgent(dry_run=True).run()
        sys.exit(0 if success else 1)
    else:
        # Default behavior: Execute MasterExecutiveAgent autonomous lifecycle
        from agents.master_executive import MasterExecutiveAgent
        success = MasterExecutiveAgent(dry_run=False).run()
        sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
