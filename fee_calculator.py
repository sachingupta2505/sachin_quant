"""
Indian Regulatory Fee Engine for NSE Derivatives & Angel One
Module: fee_calculator.py

Implements statutory and broker charges for Nifty Options:
1. Brokerage: Flat ₹20 per executed order (leg)
2. Securities Transaction Tax (STT): 0.1% (0.001) on premium sell turnover (revised option norms)
3. Exchange Turnover Charges (ETC): 0.05% (0.0005) on total turnover (NSE rate)
4. SEBI Turnover Charges: ₹10 per crore (0.000001) on total turnover
5. Stamp Duty: 0.003% (0.00003) on buy-side turnover
6. Goods and Services Tax (GST): 18% (0.18) on (Brokerage + Exchange Charges + SEBI Charges)
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union

CONFIG_PATH = Path(__file__).resolve().parent / "config.json"


@dataclass
class FeeBreakdown:
    brokerage: float
    stt: float
    etc: float
    sebi: float
    stamp_duty: float
    gst: float
    total_charges: float
    buy_turnover: float
    sell_turnover: float
    total_turnover: float

    def to_dict(self) -> dict[str, float]:
        return {
            "brokerage": self.brokerage,
            "stt": self.stt,
            "etc": self.etc,
            "sebi": self.sebi,
            "stamp_duty": self.stamp_duty,
            "gst": self.gst,
            "total_charges": self.total_charges,
            "buy_turnover": self.buy_turnover,
            "sell_turnover": self.sell_turnover,
            "total_turnover": self.total_turnover,
        }


class IndianRegulatoryFeeCalculator:
    """
    Computes exact Indian regulatory and broker charges for derivative trades
    according to current SEBI, NSE, and Angel One tariff schedules.
    """

    def __init__(
        self,
        config_path: Optional[Union[str, Path]] = None,
        starting_capital: float = 100000.0,
        brokerage_per_order: float = 20.0,
        stt_rate: float = 0.001,
        etc_rate: float = 0.0005,
        sebi_rate: float = 0.000001,
        stamp_duty_rate: float = 0.00003,
        gst_rate: float = 0.18,
    ):
        self.starting_capital = starting_capital
        self.brokerage_per_order = brokerage_per_order
        self.stt_rate = stt_rate
        self.etc_rate = etc_rate
        self.sebi_rate = sebi_rate
        self.stamp_duty_rate = stamp_duty_rate
        self.gst_rate = gst_rate

        # Load from config.json if present
        cfg_file = Path(config_path) if config_path else CONFIG_PATH
        if cfg_file.exists():
            try:
                data = json.loads(cfg_file.read_text(encoding="utf-8"))
                # Capital
                if "starting_capital" in data:
                    self.starting_capital = float(data["starting_capital"])
                elif "capital" in data and "starting_capital" in data["capital"]:
                    self.starting_capital = float(data["capital"]["starting_capital"])

                # Regulatory charges
                reg = data.get("regulatory_charges", {})
                if "brokerage_per_order" in reg:
                    self.brokerage_per_order = float(reg["brokerage_per_order"])
                if "stt_rate" in reg:
                    self.stt_rate = float(reg["stt_rate"])
                if "etc_rate" in reg:
                    self.etc_rate = float(reg["etc_rate"])
                if "sebi_rate" in reg:
                    self.sebi_rate = float(reg["sebi_rate"])
                if "stamp_duty_rate" in reg:
                    self.stamp_duty_rate = float(reg["stamp_duty_rate"])
                if "gst_rate" in reg:
                    self.gst_rate = float(reg["gst_rate"])
            except Exception:
                pass

    def calculate_detailed_breakdown(
        self,
        buy_premium: float,
        sell_premium: float,
        quantity: int,
        num_legs: int = 2,
    ) -> FeeBreakdown:
        """
        Computes detailed itemized regulatory and brokerage breakdown.

        Args:
            buy_premium: Premium in points for buy-side order(s).
            sell_premium: Premium in points for sell-side order(s).
            quantity: Total contract quantity (e.g. 65).
            num_legs: Number of executed orders (default 2 for spread entry).
        """
        buy_turnover = round(float(buy_premium) * float(quantity), 4)
        sell_turnover = round(float(sell_premium) * float(quantity), 4)
        total_turnover = round(buy_turnover + sell_turnover, 4)

        # 1. Brokerage: ₹20 per executed leg/order
        brokerage = round(self.brokerage_per_order * num_legs, 2)

        # 2. STT: 0.1% on sell-side turnover for options
        stt = round(sell_turnover * self.stt_rate, 2)

        # 3. Exchange Turnover Charges (ETC): 0.05% on total turnover
        etc = round(total_turnover * self.etc_rate, 2)

        # 4. SEBI Turnover Charges: ₹10 per crore (0.000001) on total turnover
        sebi = round(total_turnover * self.sebi_rate, 2)

        # 5. Stamp Duty: 0.003% on buy-side turnover
        stamp_duty = round(buy_turnover * self.stamp_duty_rate, 2)

        # 6. GST: 18% on (Brokerage + ETC + SEBI)
        gst = round((brokerage + etc + sebi) * self.gst_rate, 2)

        total_charges = round(brokerage + stt + etc + sebi + stamp_duty + gst, 2)

        return FeeBreakdown(
            brokerage=brokerage,
            stt=stt,
            etc=etc,
            sebi=sebi,
            stamp_duty=stamp_duty,
            gst=gst,
            total_charges=total_charges,
            buy_turnover=buy_turnover,
            sell_turnover=sell_turnover,
            total_turnover=total_turnover,
        )

    def calculate_charges(
        self,
        buy_premium: float,
        sell_premium: float,
        quantity: int,
        num_legs: int = 2,
    ) -> float:
        """
        Calculates total taxes and charges in INR for option spread transactions.

        Returns:
            total_taxes_and_charges (float, rounded to 2 decimal places).
        """
        breakdown = self.calculate_detailed_breakdown(
            buy_premium=buy_premium,
            sell_premium=sell_premium,
            quantity=quantity,
            num_legs=num_legs,
        )
        return breakdown.total_charges

    def calculate_spread_roundtrip_charges(
        self,
        buy_entry_premium: float,
        sell_entry_premium: float,
        buy_exit_premium: float,
        sell_exit_premium: float,
        quantity: int,
    ) -> FeeBreakdown:
        """
        Calculates full round-trip charges for a 2-leg option spread (4 executed orders total).
        - Entry: BUY hedge @ buy_entry_premium, SELL short @ sell_entry_premium
        - Exit: SELL hedge @ buy_exit_premium, BUY short @ sell_exit_premium
        """
        # Buy turnover: Entry buy hedge + Exit short leg repurchase
        total_buy_prem = buy_entry_premium + sell_exit_premium
        # Sell turnover: Entry short leg sell + Exit hedge unwinding
        total_sell_prem = sell_entry_premium + buy_exit_premium

        return self.calculate_detailed_breakdown(
            buy_premium=total_buy_prem,
            sell_premium=total_sell_prem,
            quantity=quantity,
            num_legs=4,
        )
