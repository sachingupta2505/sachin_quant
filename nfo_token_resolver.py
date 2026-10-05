"""
NFO Instrument Master Token Resolver for Angel One SmartAPI
Module: nfo_token_resolver.py

Responsibilities:
1. Download and cache Angel One OpenAPIScripMaster from:
   https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json
2. Store locally at data/nfo_instruments.json (refreshed daily if file modified date != today).
3. Filter and index only NFO segment symbols for NIFTY.
4. Method resolve_token(symbol, strike, option_type, expiry_date) -> Tuple[str, str]:
   - Returns (trading_symbol, symbol_token).
   - If expiry_date is None, auto-selects nearest weekly/monthly active expiry.
   - Provides graceful mock/fallback resolution when offline or in paper trading mode.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union
import urllib.request
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
logger = logging.getLogger("nfo_token_resolver")

DEFAULT_MASTER_URL = (
    "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
)
DEFAULT_CACHE_PATH = Path(__file__).resolve().parent / "data" / "nfo_instruments.json"


def parse_strike_price(raw_strike: Any) -> float:
    """Parses strike price safely. Angel One scrip master strikes are often in paise (strike * 100)."""
    try:
        val = float(raw_strike)
        if val > 100000.0:
            return round(val / 100.0, 2)
        return round(val, 2)
    except Exception:
        return 0.0


def parse_expiry_date(raw_expiry: Any) -> Optional[date]:
    """Parses various expiry formats (e.g. 08OCT2026, 29-OCT-2026, 2026-10-29)."""
    if not raw_expiry:
        return None
    raw = str(raw_expiry).strip().upper()
    for fmt in ("%d%b%Y", "%d-%b-%Y", "%Y-%m-%d", "%d%B%Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


class NFOTokenResolver:
    """
    Dynamic token and trading symbol resolver for NSE Nifty derivative contracts (NFO).
    Caches Angel One Scrip Master locally to data/nfo_instruments.json and updates daily.
    """

    def __init__(
        self,
        cache_path: Optional[Union[str, Path]] = None,
        master_url: str = DEFAULT_MASTER_URL,
        auto_load: bool = True,
        force_refresh: bool = False,
    ):
        self.cache_path = Path(cache_path) if cache_path else DEFAULT_CACHE_PATH
        self.master_url = master_url
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)

        self._instruments: List[Dict[str, Any]] = []
        # Key: (strike, option_type) -> List of (expiry_date, trading_symbol, symbol_token, expiry_str)
        self._strike_options: Dict[Tuple[float, str], List[Tuple[date, str, str, str]]] = {}
        # Key: (strike, option_type, expiry_str_upper) -> (trading_symbol, symbol_token)
        self._exact_index: Dict[Tuple[float, str, str], Tuple[str, str]] = {}

        if auto_load:
            self.load_instruments(force_refresh=force_refresh)

    def is_cache_fresh(self) -> bool:
        """Returns True if the local cache exists and was modified today (in IST)."""
        if not self.cache_path.exists():
            return False
        try:
            mtime = datetime.fromtimestamp(self.cache_path.stat().st_mtime, tz=IST)
            today = datetime.now(IST).date()
            return mtime.date() == today and self.cache_path.stat().st_size > 0
        except Exception:
            return False

    def load_instruments(self, force_refresh: bool = False) -> bool:
        """
        Loads NIFTY NFO instruments from local cache if fresh today,
        otherwise downloads from OpenAPIScripMaster, caches locally, and indexes.
        """
        # Step 1: Check today's cache
        if not force_refresh and self.is_cache_fresh():
            try:
                if self._load_from_cache():
                    logger.info(
                        f"[NFOTokenResolver] Loaded {len(self._instruments)} cached NIFTY NFO instruments from {self.cache_path}."
                    )
                    return True
            except Exception as e:
                logger.warning(
                    f"[NFOTokenResolver] Error reading cache {self.cache_path}: {e}. Refreshing..."
                )

        # Step 2: Download and cache fresh master
        try:
            if self._download_and_cache():
                logger.info(
                    f"[NFOTokenResolver] Downloaded and cached {len(self._instruments)} NIFTY NFO instruments to {self.cache_path}."
                )
                return True
        except Exception as e:
            logger.warning(
                f"[NFOTokenResolver] Failed to download scrip master from {self.master_url}: {e}."
            )

        # Step 3: Fallback to existing cache file even if from an earlier day
        if self.cache_path.exists() and self.cache_path.stat().st_size > 0:
            try:
                if self._load_from_cache():
                    logger.info(
                        f"[NFOTokenResolver] Falling back to existing cache ({len(self._instruments)} instruments)."
                    )
                    return True
            except Exception as e:
                logger.error(f"[NFOTokenResolver] Fallback cache load failed: {e}")

        logger.warning(
            "[NFOTokenResolver] No instrument cache available. Operating in synthetic fallback mode."
        )
        return False

    def _load_from_cache(self) -> bool:
        content = self.cache_path.read_text(encoding="utf-8")
        data = json.loads(content)
        if isinstance(data, dict) and "instruments" in data:
            raw_instruments = data["instruments"]
        elif isinstance(data, list):
            raw_instruments = data
        else:
            return False

        self._instruments = raw_instruments
        self._build_index()
        return len(self._instruments) > 0

    def _download_and_cache(self, timeout_sec: int = 20) -> bool:
        """Downloads OpenAPIScripMaster, filters NFO NIFTY symbols, and saves locally."""
        req = urllib.request.Request(
            self.master_url,
            headers={"User-Agent": "SachinQuant/1.0 (Mozilla/5.0)"},
        )
        with urllib.request.urlopen(req, timeout=timeout_sec) as response:
            raw_data = json.load(response)

        filtered = []
        for item in raw_data:
            if (
                item.get("exch_seg") == "NFO"
                and item.get("name") == "NIFTY"
                and item.get("instrumenttype") in ("OPTIDX", "FUTIDX")
            ):
                token = str(item.get("token", "")).strip()
                symbol = str(item.get("symbol", "")).strip()
                expiry = str(item.get("expiry", "")).strip()
                strike = parse_strike_price(item.get("strike", 0.0))
                opt_type = (
                    "CE" if symbol.endswith("CE") else ("PE" if symbol.endswith("PE") else "")
                )

                filtered.append({
                    "token": token,
                    "symbol": symbol,
                    "name": "NIFTY",
                    "expiry": expiry,
                    "strike": strike,
                    "lotsize": int(item.get("lotsize", 25)),
                    "instrumenttype": item.get("instrumenttype", "OPTIDX"),
                    "exch_seg": "NFO",
                    "option_type": opt_type,
                })

        if not filtered:
            return False

        cache_payload = {
            "updated_at": datetime.now(IST).isoformat(),
            "count": len(filtered),
            "instruments": filtered,
        }
        self.cache_path.write_text(json.dumps(cache_payload, indent=2), encoding="utf-8")
        self._instruments = filtered
        self._build_index()
        return True

    def _build_index(self) -> None:
        """Builds in-memory lookup indexes for fast O(1) resolution."""
        self._strike_options.clear()
        self._exact_index.clear()

        for item in self._instruments:
            token = str(item["token"])
            symbol = str(item["symbol"])
            strike = float(item["strike"])
            opt_type = str(item.get("option_type", "")).upper()
            expiry_str = str(item.get("expiry", "")).strip().upper()
            expiry_date = parse_expiry_date(expiry_str)

            if not opt_type or not expiry_date:
                continue

            pair_key = (strike, opt_type)
            if pair_key not in self._strike_options:
                self._strike_options[pair_key] = []
            self._strike_options[pair_key].append((expiry_date, symbol, token, expiry_str))

            # Exact match indexes
            self._exact_index[(strike, opt_type, expiry_str)] = (symbol, token)
            self._exact_index[(strike, opt_type, expiry_date.strftime("%Y-%m-%d"))] = (symbol, token)
            self._exact_index[(strike, opt_type, expiry_date.strftime("%d-%b-%Y").upper())] = (
                symbol,
                token,
            )

        # Sort all options by expiry date ascending
        for pair_key in self._strike_options:
            self._strike_options[pair_key].sort(key=lambda x: x[0])

    def resolve_token(
        self,
        symbol: str = "NIFTY",
        strike: float = 25000.0,
        option_type: str = "CE",
        expiry_date: Optional[str] = None,
    ) -> Tuple[str, str]:
        """
        Resolves trading_symbol and symbol_token for a NIFTY option.
        If expiry_date is None, auto-selects the nearest active weekly or monthly expiry.
        Falls back to a deterministic synthetic symbol and token when offline or unmatched.
        """
        target_strike = round(float(strike), 2)
        opt_type = option_type.upper().strip()
        if opt_type not in ("CE", "PE"):
            opt_type = "PE" if "PE" in symbol.upper() else "CE"

        pair_key = (target_strike, opt_type)
        today = datetime.now(IST).date()

        # 1. Exact expiry lookup if requested
        if expiry_date and pair_key in self._strike_options:
            exp_norm = str(expiry_date).strip().upper()
            if (target_strike, opt_type, exp_norm) in self._exact_index:
                return self._exact_index[(target_strike, opt_type, exp_norm)]

            # Check if expiry_date string matches date
            target_date = parse_expiry_date(exp_norm)
            if target_date:
                for d, s, t, _ in self._strike_options[pair_key]:
                    if d == target_date:
                        return s, t

        # 2. Nearest active expiry auto-selection
        if pair_key in self._strike_options:
            candidates = [c for c in self._strike_options[pair_key] if c[0] >= today]
            if candidates:
                # First candidate is nearest active expiry
                nearest = candidates[0]
                return nearest[1], nearest[2]
            elif self._strike_options[pair_key]:
                # If all expired or mock, return last available
                latest = self._strike_options[pair_key][-1]
                return latest[1], latest[2]

        # 3. Graceful Mock / Fallback Resolution
        return self._generate_synthetic_token(
            symbol=symbol,
            strike=target_strike,
            option_type=opt_type,
            expiry_date=expiry_date,
        )

    def _generate_synthetic_token(
        self,
        symbol: str,
        strike: float,
        option_type: str,
        expiry_date: Optional[str] = None,
    ) -> Tuple[str, str]:
        """Generates deterministic realistic symbol and token when offline or in paper mode."""
        today = datetime.now(IST).date()

        if expiry_date:
            parsed_d = parse_expiry_date(expiry_date)
            target_d = parsed_d if parsed_d else today
        else:
            # Nearest Thursday (NSE weekly options expiry)
            days_ahead = (3 - today.weekday()) % 7
            if days_ahead == 0 and datetime.now(IST).hour >= 15:
                days_ahead = 7
            target_d = today + timedelta(days=days_ahead)

        exp_tag = target_d.strftime("%d%b%y").upper()
        clean_strike = int(round(strike))
        opt_type = option_type.upper().strip()

        trading_symbol = f"NIFTY{exp_tag}{clean_strike}{opt_type}"
        # Deterministic positive numerical token
        token_num = 40000 + (clean_strike % 2000) * 10 + (1 if opt_type == "CE" else 2)
        symbol_token = str(token_num)

        return trading_symbol, symbol_token
