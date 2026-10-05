"""
Execution Engine Module for Algorithmic Trading Engine
Features:
1. Angel One SmartAPI integration with TOTP authentication (pyotp).
2. Support & Resistance zones calculation.
3. 5-min rejection candle detection (wick ratio >= 50%).
4. Defined-risk option spread executor (Paper Trading toggle enabled by default).
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Optional, Sequence
from zoneinfo import ZoneInfo

import pyotp
from SmartApi import SmartConnect

from regime_filter import Candle, InitialBalance
from risk_guard import RiskGuard, RiskState
from nfo_token_resolver import NFOTokenResolver

logger = logging.getLogger("execution_engine")
IST = ZoneInfo("Asia/Kolkata")
NIFTY_LOT_SIZE = 25  # Official NSE Nifty derivatives lot size
MAX_SPREAD_RISK_INR = 1500.0  # Hard single-spread risk ceiling (aligned with daily kill-switch)


class SignalType(str, Enum):
    BULLISH_REJECTION = "BULLISH_REJECTION"  # Long wick rejected at Support
    BEARISH_REJECTION = "BEARISH_REJECTION"  # Long wick rejected at Resistance
    NONE = "NONE"


class SpreadType(str, Enum):
    BULL_PUT_SPREAD = "BULL_PUT_SPREAD"      # Bullish: Sell ATM Put, Buy OTM Put hedge
    BEAR_CALL_SPREAD = "BEAR_CALL_SPREAD"    # Bearish: Sell ATM Call, Buy OTM Call hedge


@dataclass
class SRZone:
    """Support or Resistance price level with upper and lower tolerance bands."""
    name: str
    level: float
    band_pts: float = 12.0  # +/- 12 points buffer on Nifty

    @property
    def upper(self) -> float:
        return self.level + self.band_pts

    @property
    def lower(self) -> float:
        return self.level - self.band_pts

    def touches_or_penetrates(self, low: float, high: float) -> bool:
        return not (high < self.lower or low > self.upper)


@dataclass
class RejectionSignal:
    signal_type: SignalType
    candle: Candle
    wick_ratio: float
    zone: SRZone
    confidence: float
    description: str


@dataclass
class SpreadLeg:
    symbol: str
    strike: float
    option_type: str  # "CE" or "PE"
    action: str       # "BUY" or "SELL"
    quantity: int
    price: float
    order_id: Optional[str] = None


@dataclass
class SpreadTrade:
    trade_id: str
    spread_type: SpreadType
    legs: list[SpreadLeg]
    net_credit: float
    max_risk_inr: float
    max_reward_inr: float
    timestamp: datetime
    is_paper: bool = True
    status: str = "OPEN"
    metadata: dict[str, Any] = field(default_factory=dict)


class AngelAuth:
    """
    Manages authentication with Angel One SmartAPI using TOTP generation.
    Supports mock/paper session for offline testing or credentials-free simulation.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        client_code: Optional[str] = None,
        pin: Optional[str] = None,
        totp_secret: Optional[str] = None,
    ):
        self.api_key = api_key
        self.client_code = client_code
        self.pin = pin
        self.totp_secret = totp_secret
        self.smart_api: Optional[SmartConnect] = None
        self.auth_token: Optional[str] = None
        self.feed_token: Optional[str] = None
        self.refresh_token: Optional[str] = None
        self.is_authenticated: bool = False

    def generate_totp(self) -> str:
        if not self.totp_secret:
            raise ValueError("TOTP secret not configured")
        totp = pyotp.TOTP(self.totp_secret)
        return totp.now()

    def login(self) -> bool:
        """
        Authenticates with Angel One SmartAPI using TOTP.
        """
        if not all([self.api_key, self.client_code, self.pin, self.totp_secret]):
            logger.warning("Angel One credentials incomplete. Operating in Mock/Paper authentication mode.")
            self.is_authenticated = False
            return False

        try:
            self.smart_api = SmartConnect(api_key=self.api_key)
            totp_code = self.generate_totp()
            data = self.smart_api.generateSession(self.client_code, self.pin, totp_code)

            if data and data.get("status"):
                self.auth_token = data["data"]["jwtToken"]
                self.feed_token = data["data"]["feedToken"]
                self.refresh_token = data["data"]["refreshToken"]
                self.is_authenticated = True
                logger.info(f"Successfully authenticated with Angel One SmartAPI for {self.client_code}")
                return True
            else:
                logger.error(f"Angel One login failed: {data}")
                self.is_authenticated = False
                return False
        except Exception as e:
            logger.error(f"Exception during Angel One authentication: {e}")
            self.is_authenticated = False
            return False


class RejectionDetector:
    """
    Detects 5-minute rejection candles (wick ratio >= 50%) at Key S/R zones.
    """

    def __init__(self, min_wick_ratio: float = 0.50, min_candle_range: float = 12.0):
        self.min_wick_ratio = min_wick_ratio
        self.min_candle_range = min_candle_range  # Minimum range (in pts) to filter out micro-noise bars

    def identify_sr_zones(
        self,
        ib: Optional[InitialBalance],
        spot_price: float,
        swing_highs: Optional[Sequence[float]] = None,
        swing_lows: Optional[Sequence[float]] = None,
        daily_levels: Optional[dict[str, float]] = None,
        weekly_levels: Optional[dict[str, float]] = None,
        pdh: Optional[float] = None,
        pdl: Optional[float] = None,
        pdc: Optional[float] = None,
        pwh: Optional[float] = None,
        pwl: Optional[float] = None,
    ) -> list[SRZone]:
        """
        Computes key Support & Resistance zones using Initial Balance (IBH/IBL),
        Daily/Weekly higher timeframe reference levels (PDH, PDL, PDC, PWH, PWL),
        and psychological/strike levels (multiples of 50/100).
        """
        zones: list[SRZone] = []

        if ib is not None:
            zones.append(SRZone(name="IB_HIGH_RESISTANCE", level=ib.high))
            zones.append(SRZone(name="IB_LOW_SUPPORT", level=ib.low))

        # Higher Timeframe Reference Levels (Daily: PDH, PDL, PDC)
        if daily_levels:
            pdh = daily_levels.get("pdh", daily_levels.get("PDH", pdh))
            pdl = daily_levels.get("pdl", daily_levels.get("PDL", pdl))
            pdc = daily_levels.get("pdc", daily_levels.get("PDC", pdc))

        # Higher Timeframe Reference Levels (Weekly: PWH, PWL)
        if weekly_levels:
            pwh = weekly_levels.get("pwh", weekly_levels.get("PWH", pwh))
            pwl = weekly_levels.get("pwl", weekly_levels.get("PWL", pwl))

        if pdh is not None and float(pdh) > 0:
            zones.append(SRZone(name="DAILY_HIGH_RES", level=float(pdh)))
        if pdl is not None and float(pdl) > 0:
            zones.append(SRZone(name="DAILY_LOW_SUP", level=float(pdl)))
        if pdc is not None and float(pdc) > 0:
            zones.append(SRZone(name="DAILY_CLOSE_PIVOT", level=float(pdc)))

        if pwh is not None and float(pwh) > 0:
            zones.append(SRZone(name="WEEKLY_HIGH_RES", level=float(pwh)))
        if pwl is not None and float(pwl) > 0:
            zones.append(SRZone(name="WEEKLY_LOW_SUP", level=float(pwl)))

        # Psychological / Strike boundaries (nearest 50-pt strikes around spot)
        base = round(spot_price / 50.0) * 50.0
        zones.append(SRZone(name=f"STRIKE_RES_{int(base + 50)}", level=base + 50.0))
        zones.append(SRZone(name=f"STRIKE_SUP_{int(base - 50)}", level=base - 50.0))

        if swing_highs:
            for i, sh in enumerate(swing_highs):
                zones.append(SRZone(name=f"SWING_HIGH_{i+1}", level=sh))
        if swing_lows:
            for i, sl in enumerate(swing_lows):
                zones.append(SRZone(name=f"SWING_LOW_{i+1}", level=sl))

        return zones

    def detect_rejection(
        self,
        candle: Candle,
        zones: Sequence[SRZone],
    ) -> RejectionSignal:
        """
        Detects if a 5-minute candle forms a high-probability rejection:
        - Candle range >= min_candle_range (avoid noise)
        - Rejection wick / range >= 50%
        - Rejection wick tests an S/R zone
        - Close confirms rejection direction
        """
        c_range = candle.range
        if c_range < self.min_candle_range:
            return RejectionSignal(
                signal_type=SignalType.NONE,
                candle=candle,
                wick_ratio=0.0,
                zone=SRZone(name="NONE", level=0.0),
                confidence=0.0,
                description="Candle range below minimum threshold.",
            )

        # 1. Check Bullish Rejection (Hammer / Pin bar at Support)
        lower_wick_ratio = candle.lower_wick / c_range
        if lower_wick_ratio >= self.min_wick_ratio:
            # Must close in upper 50% of the bar
            if candle.close >= (candle.low + 0.5 * c_range):
                for zone in zones:
                    # Candle dipped into or tested the support zone
                    if zone.touches_or_penetrates(candle.low, candle.low + candle.lower_wick):
                        return RejectionSignal(
                            signal_type=SignalType.BULLISH_REJECTION,
                            candle=candle,
                            wick_ratio=round(lower_wick_ratio, 3),
                            zone=zone,
                            confidence=min(round(lower_wick_ratio * 1.2, 2), 1.0),
                            description=(
                                f"Bullish Rejection at {zone.name} ({zone.level:.1f}): "
                                f"Lower wick {candle.lower_wick:.1f} pts is {lower_wick_ratio * 100:.1f}% "
                                f"of candle range ({c_range:.1f} pts)."
                            ),
                        )

        # 2. Check Bearish Rejection (Shooting Star / Pin bar at Resistance)
        upper_wick_ratio = candle.upper_wick / c_range
        if upper_wick_ratio >= self.min_wick_ratio:
            # Must close in lower 50% of the bar
            if candle.close <= (candle.high - 0.5 * c_range):
                for zone in zones:
                    # Candle spiked into or tested the resistance zone
                    if zone.touches_or_penetrates(candle.high - candle.upper_wick, candle.high):
                        return RejectionSignal(
                            signal_type=SignalType.BEARISH_REJECTION,
                            candle=candle,
                            wick_ratio=round(upper_wick_ratio, 3),
                            zone=zone,
                            confidence=min(round(upper_wick_ratio * 1.2, 2), 1.0),
                            description=(
                                f"Bearish Rejection at {zone.name} ({zone.level:.1f}): "
                                f"Upper wick {candle.upper_wick:.1f} pts is {upper_wick_ratio * 100:.1f}% "
                                f"of candle range ({c_range:.1f} pts)."
                            ),
                        )

        return RejectionSignal(
            signal_type=SignalType.NONE,
            candle=candle,
            wick_ratio=max(lower_wick_ratio, upper_wick_ratio),
            zone=SRZone(name="NONE", level=0.0),
            confidence=0.0,
            description="No rejection criteria met.",
        )


def fetch_historical_reference_levels(
    auth: Optional[Any] = None,
    symbol_token: str = "99926000",
    cache_file: str = "daily_reference_levels.json",
    fallback_spot: Optional[float] = None,
) -> dict[str, Any]:
    """
    Fetches previous day (PDH, PDL, PDC) and previous week (PWH, PWL) reference levels
    using Angel One SmartAPI getCandleData with robust fallback to a local JSON cache.
    """
    cache_path = Path(cache_file)
    if not cache_path.is_absolute():
        cache_path = Path(__file__).resolve().parent / cache_file

    smart_api = None
    if auth is not None:
        if hasattr(auth, "getCandleData"):
            smart_api = auth
        elif hasattr(auth, "smart_api"):
            smart_api = auth.smart_api

    # Attempt 1: Fetch live historical candle data via SmartAPI
    if smart_api is not None:
        try:
            now = datetime.now(IST)
            from_date = (now - timedelta(days=35)).strftime("%Y-%m-%d 09:15")
            to_date = now.strftime("%Y-%m-%d 15:30")
            payload = {
                "exchange": "NSE",
                "symboltoken": str(symbol_token),
                "interval": "ONE_DAY",
                "fromdate": from_date,
                "todate": to_date,
            }
            resp = smart_api.getCandleData(payload)
            if resp and isinstance(resp, dict) and resp.get("status") and resp.get("data"):
                raw_candles = resp["data"]
                # Candle schema: [timestamp, open, high, low, close, volume]
                # Filter strictly past trading days (prior to today in IST)
                today_date = now.date()
                parsed_candles = []
                for c in raw_candles:
                    if not c or len(c) < 5:
                        continue
                    ts_str = str(c[0])
                    try:
                        clean_ts = ts_str.split("T")[0].split(" ")[0]
                        c_date = datetime.strptime(clean_ts, "%Y-%m-%d").date()
                    except Exception:
                        continue
                    if c_date < today_date:
                        parsed_candles.append({
                            "date": c_date,
                            "open": float(c[1]),
                            "high": float(c[2]),
                            "low": float(c[3]),
                            "close": float(c[4]),
                        })

                if parsed_candles:
                    # Last completed trading day
                    prev_day = parsed_candles[-1]
                    pdh = round(prev_day["high"], 2)
                    pdl = round(prev_day["low"], 2)
                    pdc = round(prev_day["close"], 2)

                    # Determine Previous Week (Monday to Friday of previous week)
                    cur_monday = today_date - timedelta(days=today_date.weekday())
                    prev_monday = cur_monday - timedelta(days=7)
                    prev_week_candles = [
                        c for c in parsed_candles
                        if prev_monday <= c["date"] < cur_monday
                    ]
                    if not prev_week_candles:
                        prev_week_candles = [c for c in parsed_candles if c["date"] < cur_monday][-5:]
                    if not prev_week_candles:
                        prev_week_candles = parsed_candles[-5:]

                    pwh = round(max(c["high"] for c in prev_week_candles), 2)
                    pwl = round(min(c["low"] for c in prev_week_candles), 2)

                    data = {
                        "pdh": pdh,
                        "pdl": pdl,
                        "pdc": pdc,
                        "pwh": pwh,
                        "pwl": pwl,
                        "daily": {"pdh": pdh, "pdl": pdl, "pdc": pdc},
                        "weekly": {"pwh": pwh, "pwl": pwl},
                        "timestamp": now.isoformat(),
                        "source": "SMART_API",
                    }
                    try:
                        cache_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
                        logger.info(f"Updated S/R reference levels cache at {cache_path}")
                    except Exception as we:
                        logger.warning(f"Could not persist levels to cache {cache_path}: {we}")
                    return data
        except Exception as e:
            logger.warning(f"Error fetching historical candles via SmartAPI: {e}. Falling back to cache.")

    # Attempt 2: Load from local JSON cache file
    if cache_path.exists():
        try:
            content = cache_path.read_text(encoding="utf-8")
            cached = json.loads(content)
            if all(k in cached for k in ("pdh", "pdl", "pdc", "pwh", "pwl")):
                cached["daily"] = cached.get("daily", {"pdh": cached["pdh"], "pdl": cached["pdl"], "pdc": cached["pdc"]})
                cached["weekly"] = cached.get("weekly", {"pwh": cached["pwh"], "pwl": cached["pwl"]})
                cached["source"] = "LOCAL_CACHE"
                logger.info(f"Loaded S/R reference levels from cache {cache_path}: PDH={cached['pdh']}, PDL={cached['pdl']}")
                return cached
        except Exception as e:
            logger.warning(f"Failed to read cache file {cache_path}: {e}")

    # Attempt 3: Safe synthetic fallback (around spot price or 25000)
    base_spot = fallback_spot if (fallback_spot is not None and fallback_spot > 0) else 25000.0
    pdh = round(base_spot + 120.0, 2)
    pdl = round(base_spot - 120.0, 2)
    pdc = round(base_spot + 15.0, 2)
    pwh = round(base_spot + 280.0, 2)
    pwl = round(base_spot - 280.0, 2)
    fallback_data = {
        "pdh": pdh,
        "pdl": pdl,
        "pdc": pdc,
        "pwh": pwh,
        "pwl": pwl,
        "daily": {"pdh": pdh, "pdl": pdl, "pdc": pdc},
        "weekly": {"pwh": pwh, "pwl": pwl},
        "timestamp": datetime.now(IST).isoformat(),
        "source": "FALLBACK_DEFAULT",
    }
    try:
        cache_path.write_text(json.dumps(fallback_data, indent=2), encoding="utf-8")
    except Exception:
        pass
    return fallback_data


class ExecutionEngine:
    """
    Defined-risk option spread executor.
    Supports paper trading (default) and live Angel One SmartAPI order routing.
    Strictly gates all executions with RiskGuard.
    """

    def __init__(
        self,
        risk_guard: RiskGuard,
        auth: Optional[AngelAuth] = None,
        paper_trading: bool = True,  # Paper trading enabled by default
        lot_size: int = NIFTY_LOT_SIZE,
        spread_width_pts: float = 50.0,
        tz: ZoneInfo = IST,
        token_resolver: Optional[NFOTokenResolver] = None,
    ):
        self.risk_guard = risk_guard
        self.auth = auth or AngelAuth()
        self.paper_trading = paper_trading
        self.lot_size = lot_size
        self.spread_width_pts = spread_width_pts
        self.tz = tz
        self.token_resolver = token_resolver or NFOTokenResolver()
        self.active_spread: Optional[SpreadTrade] = None
        self.executed_trades: list[SpreadTrade] = []

    def build_spread(
        self,
        signal: RejectionSignal,
        spot_price: float,
        timestamp: Optional[datetime] = None,
    ) -> Optional[SpreadTrade]:
        """
        Constructs a defined-risk 2-leg credit spread based on rejection signal:
        - Bullish Rejection -> Bull Put Spread:
          * Sell ATM/near-OTM Put (Strike K)
          * Buy OTM Put hedge (Strike K - 50)
        - Bearish Rejection -> Bear Call Spread:
          * Sell ATM/near-OTM Call (Strike K)
          * Buy OTM Call hedge (Strike K + 50)
        """
        if signal.signal_type == SignalType.NONE:
            return None

        dt = timestamp or signal.candle.timestamp
        trade_id = f"SPD-{dt.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4].upper()}"

        # Nearest 50-pt strike
        atm_strike = round(spot_price / 50.0) * 50.0

        # Dynamic strike width selection respecting MAX_SPREAD_RISK_INR <= 1500.0
        candidate_widths = [self.spread_width_pts]
        if self.spread_width_pts > 50.0:
            candidate_widths.append(50.0)

        chosen_width: Optional[float] = None
        chosen_net_credit: float = 0.0
        chosen_max_risk_inr: float = 0.0

        for width in candidate_widths:
            sell_prem = 75.0
            buy_prem = max(57.0 - (width - 50.0) * 0.25, 20.0)
            net_credit = sell_prem - buy_prem
            risk_inr = (width * self.lot_size) - (net_credit * self.lot_size)

            if risk_inr <= MAX_SPREAD_RISK_INR:
                chosen_width = width
                chosen_net_credit = net_credit
                chosen_max_risk_inr = round(risk_inr, 2)
                break
            else:
                logger.warning(
                    f"[RISK ADAPTATION] Width {width} pts yields Max Risk INR {risk_inr:.2f} > "
                    f"limit INR {MAX_SPREAD_RISK_INR:.2f}. Attempting tighter strike width..."
                )

        if chosen_width is None:
            logger.error(
                f"[TRADE FORMULATION REJECTED] Cannot construct defined-risk spread within "
                f"hard limit of INR {MAX_SPREAD_RISK_INR:.2f} (Lot size: {self.lot_size}). "
                f"Trade proposal aborted."
            )
            return None

        if signal.signal_type == SignalType.BULLISH_REJECTION:
            # Bull Put Spread: Sell Put at atm_strike, Buy Put at atm_strike - chosen_width
            sell_strike = atm_strike
            buy_strike = sell_strike - chosen_width
            sell_prem = 75.0
            buy_prem = sell_prem - chosen_net_credit

            legs = [
                SpreadLeg(
                    symbol=f"NIFTY_{int(sell_strike)}_PE",
                    strike=sell_strike,
                    option_type="PE",
                    action="SELL",
                    quantity=self.lot_size,
                    price=sell_prem,
                ),
                SpreadLeg(
                    symbol=f"NIFTY_{int(buy_strike)}_PE",
                    strike=buy_strike,
                    option_type="PE",
                    action="BUY",
                    quantity=self.lot_size,
                    price=buy_prem,
                ),
            ]

            max_reward_inr = round(chosen_net_credit * self.lot_size, 2)

            return SpreadTrade(
                trade_id=trade_id,
                spread_type=SpreadType.BULL_PUT_SPREAD,
                legs=legs,
                net_credit=chosen_net_credit,
                max_risk_inr=chosen_max_risk_inr,
                max_reward_inr=max_reward_inr,
                timestamp=dt,
                is_paper=self.paper_trading,
                metadata={"signal": signal.description, "spot": spot_price, "width": chosen_width},
            )

        elif signal.signal_type == SignalType.BEARISH_REJECTION:
            # Bear Call Spread: Sell Call at atm_strike, Buy Call at atm_strike + chosen_width
            sell_strike = atm_strike
            buy_strike = sell_strike + chosen_width
            sell_prem = 75.0
            buy_prem = sell_prem - chosen_net_credit

            legs = [
                SpreadLeg(
                    symbol=f"NIFTY_{int(sell_strike)}_CE",
                    strike=sell_strike,
                    option_type="CE",
                    action="SELL",
                    quantity=self.lot_size,
                    price=sell_prem,
                ),
                SpreadLeg(
                    symbol=f"NIFTY_{int(buy_strike)}_CE",
                    strike=buy_strike,
                    option_type="CE",
                    action="BUY",
                    quantity=self.lot_size,
                    price=buy_prem,
                ),
            ]

            max_reward_inr = round(chosen_net_credit * self.lot_size, 2)

            return SpreadTrade(
                trade_id=trade_id,
                spread_type=SpreadType.BEAR_CALL_SPREAD,
                legs=legs,
                net_credit=chosen_net_credit,
                max_risk_inr=chosen_max_risk_inr,
                max_reward_inr=max_reward_inr,
                timestamp=dt,
                is_paper=self.paper_trading,
                metadata={"signal": signal.description, "spot": spot_price, "width": chosen_width},
            )

        return None

    def execute_spread(self, spread: SpreadTrade) -> bool:
        """
        Executes a 2-leg defined-risk spread trade:
        1. Checks RiskGuard permission.
        2. Routes order (paper simulation or Angel One API).
        3. Updates RiskGuard with trade entry.
        """
        allowed, reason = self.risk_guard.can_enter_trade(spread.timestamp)
        if not allowed:
            logger.warning(f"Execution rejected by RiskGuard: {reason}")
            return False

        if self.paper_trading:
            # Paper execution: Assign synthetic order IDs and record fill
            for i, leg in enumerate(spread.legs):
                leg.order_id = f"PAPER-ORD-{spread.trade_id}-{i+1}"
            spread.status = "FILLED"
            self.active_spread = spread
            self.executed_trades.append(spread)

            self.risk_guard.record_trade_entry(
                trade_id=spread.trade_id,
                details={
                    "spread_type": spread.spread_type.value,
                    "is_paper": True,
                    "net_credit": spread.net_credit,
                    "max_risk_inr": spread.max_risk_inr,
                },
                current_time=spread.timestamp,
            )
            logger.info(f"[PAPER TRADING] Executed {spread.spread_type.value} ({spread.trade_id})")
            return True

        else:
            # Live Angel One execution
            if not self.auth.is_authenticated or self.auth.smart_api is None:
                raise RuntimeError("Cannot execute live trade: Angel One SmartAPI is not authenticated")

            placed_orders: list[str] = []
            try:
                for leg in spread.legs:
                    resolved_sym, resolved_tok = self.token_resolver.resolve_token(
                        symbol="NIFTY",
                        strike=leg.strike,
                        option_type=leg.option_type,
                    )
                    order_params = {
                        "variety": "NORMAL",
                        "tradingsymbol": resolved_sym,
                        "symboltoken": resolved_tok,
                        "transactiontype": leg.action,
                        "exchange": "NFO",
                        "ordertype": "LIMIT",
                        "producttype": "INTRADAY",
                        "duration": "DAY",
                        "price": str(leg.price),
                        "quantity": str(leg.quantity),
                    }
                    resp = self.auth.smart_api.placeOrder(order_params)
                    if resp and resp.get("status"):
                        order_id = resp["data"]["orderid"]
                        leg.order_id = order_id
                        placed_orders.append(order_id)
                    else:
                        raise RuntimeError(f"Order placement failed for {leg.symbol}: {resp}")

                spread.status = "FILLED"
                self.active_spread = spread
                self.executed_trades.append(spread)

                self.risk_guard.record_trade_entry(
                    trade_id=spread.trade_id,
                    details={
                        "spread_type": spread.spread_type.value,
                        "is_paper": False,
                        "net_credit": spread.net_credit,
                    },
                    current_time=spread.timestamp,
                )
                return True

            except Exception as e:
                logger.error(f"Live order execution error, rolling back placed legs: {e}")
                # Immediate atomic rollback / cancellation for safety
                for oid in placed_orders:
                    try:
                        self.auth.smart_api.cancelOrder(oid, "NORMAL")
                    except Exception:
                        pass
                return False

    def close_active_spread(self, realized_pnl: float, exit_time: Optional[datetime] = None) -> bool:
        """
        Closes the active spread, records exit with RiskGuard, and clears position.
        """
        if self.active_spread is None:
            return False

        t_id = self.active_spread.trade_id
        self.active_spread.status = "CLOSED"
        self.risk_guard.record_trade_exit(t_id, realized_pnl=realized_pnl, current_time=exit_time)
        self.active_spread = None
        return True
