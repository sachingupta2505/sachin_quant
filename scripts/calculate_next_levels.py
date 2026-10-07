"""
Deterministic Next-Session Level Calculator for SachinQuant
Module: scripts/calculate_next_levels.py

Fetches official Nifty 50 Spot EOD candle data via Angel One SmartAPI
with fallback to local candle cache, calculates pure mathematical levels:
- PDH: Previous Day High
- PDL: Previous Day Low
- PDC: Previous Day Close
- PWH: Previous Week High
- PWL: Previous Week Low
- ATR_14: 14-period daily Average True Range
Writes deterministic output strictly to data/next_session_levels.json.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import dotenv
dotenv.load_dotenv(ROOT_DIR / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("LevelCalculator")


CACHE_FILE = ROOT_DIR / "data" / "nifty_daily_candles.json"
OUTPUT_FILE = ROOT_DIR / "data" / "next_session_levels.json"


def fetch_official_candles(symbol_token: str = "99926000", invalidate_cache: bool = False) -> List[Dict[str, Any]]:
    """
    Fetches official daily candles from Angel One SmartAPI.
    Saves to local cache and returns structured list of candles.
    """
    now = datetime.now(IST)
    candles_raw = []

    if invalidate_cache and CACHE_FILE.exists():
        logger.info(f"Invalidating stale local candle cache at {CACHE_FILE}...")
        try:
            CACHE_FILE.unlink()
        except OSError:
            pass

    try:
        from main_runner import AngelAuth
        api_key = os.getenv("SMARTAPI_API_KEY")
        client_code = os.getenv("SMARTAPI_CLIENT_CODE")
        pin = os.getenv("SMARTAPI_PIN")
        totp = os.getenv("SMARTAPI_TOTP_SECRET")

        if api_key and client_code and pin and totp:
            auth = AngelAuth(api_key=api_key, client_code=client_code, pin=pin, totp_secret=totp)
            if auth.login() and auth.smart_api:
                from_date = (now - timedelta(days=60)).strftime("%Y-%m-%d 09:15")
                to_date = now.strftime("%Y-%m-%d 15:30")
                payload = {
                    "exchange": "NSE",
                    "symboltoken": str(symbol_token),
                    "interval": "ONE_DAY",
                    "fromdate": from_date,
                    "todate": to_date,
                }
                # Retry loop with exponential backoff for rate limit protection
                for attempt in range(1, 4):
                    try:
                        time.sleep(1.0)
                        resp = auth.smart_api.getCandleData(payload)
                        if resp and isinstance(resp, dict) and resp.get("status") and resp.get("data"):
                            candles_raw = resp["data"]
                            logger.info(f"Fetched {len(candles_raw)} daily candles directly from Angel One SmartAPI (attempt {attempt}).")
                            # Persist to local cache for resilient offline parity
                            CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
                            CACHE_FILE.write_text(json.dumps(candles_raw, indent=2), encoding="utf-8")
                            break
                        else:
                            logger.warning(f"SmartAPI getCandleData attempt {attempt} returned: {resp}")
                    except Exception as attempt_err:
                        logger.warning(f"SmartAPI getCandleData attempt {attempt} exception: {attempt_err}")
                    time.sleep(1.5)
    except Exception as e:
        logger.warning(f"Angel One SmartAPI candle fetch exception: {e}")

    # Fallback strictly to local candle cache (no mock data!)
    if not candles_raw and CACHE_FILE.exists():
        logger.info(f"Loading official candles from local cache '{CACHE_FILE}'...")
        try:
            candles_raw = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        except Exception as ce:
            logger.error(f"Error reading local candle cache: {ce}")

    parsed: List[Dict[str, Any]] = []
    for c in candles_raw:
        if not c or len(c) < 5:
            continue
        try:
            c_date = datetime.strptime(str(c[0])[:10], "%Y-%m-%d").date()
            parsed.append({
                "date": c_date,
                "open": float(c[1]),
                "high": float(c[2]),
                "low": float(c[3]),
                "close": float(c[4]),
            })
        except Exception:
            continue

    if not parsed:
        raise RuntimeError("CRITICAL: Zero official candles available. Cannot calculate levels without genuine market data.")

    return parsed


def calculate_levels(target_date_str: Optional[str] = None, invalidate_cache: bool = True) -> Dict[str, Any]:
    """
    Computes mathematical technical anchors from official candles.
    """
    candles = fetch_official_candles(invalidate_cache=invalidate_cache)
    today_date = datetime.strptime(target_date_str, "%Y-%m-%d").date() if target_date_str else datetime.now(IST).date()

    # Find the candle for today or the latest available trading session
    matching_idx = None
    for i, c in enumerate(candles):
        if c["date"] <= today_date:
            matching_idx = i

    if matching_idx is None:
        raise RuntimeError(f"No candle found on or before {today_date}")

    today_candle = candles[matching_idx]
    session_date = today_candle["date"]

    pdh = round(today_candle["high"], 2)
    pdl = round(today_candle["low"], 2)
    pdc = round(today_candle["close"], 2)

    # 14-period daily Average True Range (ATR)
    relevant_candles = candles[: matching_idx + 1]
    trs: List[float] = []
    for i in range(1, len(relevant_candles)):
        prev_close = relevant_candles[i - 1]["close"]
        h = relevant_candles[i]["high"]
        l = relevant_candles[i]["low"]
        tr = max(h - l, abs(h - prev_close), abs(l - prev_close))
        trs.append(tr)

    atr_period = 14
    atr_trs = trs[-atr_period:] if len(trs) >= atr_period else trs
    atr_14 = round(sum(atr_trs) / len(atr_trs), 2) if atr_trs else round(pdh - pdl, 2)

    # Previous Week High (PWH) and Low (PWL)
    # Start of this week (Monday)
    this_monday = session_date - timedelta(days=session_date.weekday())
    prev_monday = this_monday - timedelta(days=7)
    prev_week_candles = [c for c in relevant_candles if prev_monday <= c["date"] < this_monday]
    if not prev_week_candles:
        prev_week_candles = [c for c in relevant_candles if c["date"] < this_monday][-5:]
    if not prev_week_candles:
        prev_week_candles = relevant_candles[-5:]

    pwh = round(max(c["high"] for c in prev_week_candles), 2)
    pwl = round(min(c["low"] for c in prev_week_candles), 2)

    # Next trading session date
    next_session = session_date + timedelta(days=1)
    if next_session.weekday() == 5:  # Saturday
        next_session += timedelta(days=2)
    elif next_session.weekday() == 6:  # Sunday
        next_session += timedelta(days=1)

    result = {
        "date": session_date.isoformat(),
        "next_session_date": next_session.isoformat(),
        "source": "ANGEL_ONE_OFFICIAL",
        "underlying": "NIFTY 50 SPOT",
        "pdh": pdh,
        "pdl": pdl,
        "pdc": pdc,
        "pwh": pwh,
        "pwl": pwl,
        "atr_14": atr_14,
        "daily": {"pdh": pdh, "pdl": pdl, "pdc": pdc},
        "weekly": {"pwh": pwh, "pwl": pwl},
        "atr": atr_14,
        "calculated_at": datetime.now(IST).isoformat(),
        "status": "ARMED",
    }

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text(json.dumps(result, indent=2), encoding="utf-8")
    logger.info(f"Successfully generated next session levels to {OUTPUT_FILE}")
    logger.info(f"Anchors: PDH={pdh}, PDL={pdl}, PDC={pdc}, PWH={pwh}, PWL={pwl}, ATR_14={atr_14}")
    return result


if __name__ == "__main__":
    res = calculate_levels()
    print(json.dumps(res, indent=2))
