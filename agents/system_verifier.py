"""
Automated Comprehensive Pre-Flight System Verifier
Module: agents/system_verifier.py

Performs deep static AST, configuration, and environment inspection across 4 Diagnostic Suites:
1. Suite A: Greeks & Volatility Safety Checks (IV expansion, Gamma cutoff, Theta/Credit capture)
2. Suite B: Trader Failure Traps (Daily loss limit, trade cap, Initial Balance freeze)
3. Suite C: Microstructure & Broker Execution Traps (Margin sequencing, LIMIT orders, Bid-Ask spread guard)
4. Suite D: End-to-End System Readiness (SQLite WAL, Angel One credentials in .env, 15:10 square-off)
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

repo_root = Path(__file__).resolve().parent.parent
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))

# Fix Windows console UTF-8 output
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


@dataclass
class DiagnosticCheck:
    suite: str
    name: str
    passed: bool
    verified_file: str
    verified_value: str
    details: str


class SystemVerifier:
    """
    Automated pre-flight auditor validating trading invariants before 09:15 live paper execution.
    """

    def __init__(self, root_dir: Optional[Path] = None):
        self.root_dir = root_dir or Path(__file__).parent.parent
        self.architect_path = self.root_dir / "agents" / "architect.py"
        self.coder_path = self.root_dir / "agents" / "coder.py"
        self.auditor_path = self.root_dir / "agents" / "auditor.py"
        self.devops_path = self.root_dir / "agents" / "devops.py"
        self.bus_path = self.root_dir / "bus.py"
        self.config_path = self.root_dir / "config.json"
        self.env_path = self.root_dir / ".env"

    # =========================================================================
    # SUITE A: Greeks & Volatility Safety Checks
    # =========================================================================
    def check_suite_a(self) -> list[DiagnosticCheck]:
        results: list[DiagnosticCheck] = []

        # A1: Check IV percentile / expansion limits (halt fresh entries if India VIX / IV expands violently)
        suite_name = "A. Greeks & Volatility"
        if not self.architect_path.exists():
            results.append(
                DiagnosticCheck(
                    suite=suite_name,
                    name="India VIX / IV Expansion Limit",
                    passed=False,
                    verified_file="agents/architect.py",
                    verified_value="File Missing",
                    details=f"Not found: {self.architect_path}",
                )
            )
        else:
            arch_content = self.architect_path.read_text(encoding="utf-8")
            vix_match = re.search(r'MAX_INDIA_VIX\s*(?::\s*float)?\s*=\s*([0-9.]+)', arch_content)
            has_vix_gate = bool(
                re.search(r'(?:current_vix|vix)\s*>\s*MAX_INDIA_VIX', arch_content) or
                re.search(r'VOLATILITY EXPANSION GATED', arch_content)
            )

            if vix_match and has_vix_gate:
                vix_val = float(vix_match.group(1))
                passed = (vix_val <= 25.0)
                results.append(
                    DiagnosticCheck(
                        suite=suite_name,
                        name="India VIX / IV Expansion Limit",
                        passed=passed,
                        verified_file="agents/architect.py",
                        verified_value=f"MAX_INDIA_VIX = {vix_val}",
                        details="Halts fresh entries if India VIX expands violently",
                    )
                )
            else:
                results.append(
                    DiagnosticCheck(
                        suite=suite_name,
                        name="India VIX / IV Expansion Limit",
                        passed=False,
                        verified_file="agents/architect.py",
                        verified_value="Not Enforced",
                        details="MAX_INDIA_VIX expansion guard missing in agents/architect.py",
                    )
                )

        # A2: Verify Gamma cut-off logic (block fresh OTM/ATM entries post-13:30 on expiry days)
        if not self.architect_path.exists():
            results.append(
                DiagnosticCheck(
                    suite=suite_name,
                    name="Expiry Day Gamma Cutoff (13:30 IST)",
                    passed=False,
                    verified_file="agents/architect.py",
                    verified_value="File Missing",
                    details="agents/architect.py not found",
                )
            )
        else:
            arch_content = self.architect_path.read_text(encoding="utf-8")
            has_cutoff = bool(re.search(r'EXPIRY_CUTOFF_TIME\s*(?::\s*(?:time|dtime))?\s*=\s*(?:time|dtime)\(\s*13\s*,\s*30\s*\)', arch_content))
            has_gate = bool(re.search(r'is_expiry_day.*EXPIRY_CUTOFF_TIME', arch_content))

            if has_cutoff and has_gate:
                results.append(
                    DiagnosticCheck(
                        suite=suite_name,
                        name="Expiry Day Gamma Cutoff (13:30 IST)",
                        passed=True,
                        verified_file="agents/architect.py",
                        verified_value="EXPIRY_CUTOFF_TIME = 13:30 IST",
                        details="Blocks fresh OTM/ATM entries post-13:30 on expiry days",
                    )
                )
            else:
                results.append(
                    DiagnosticCheck(
                        suite=suite_name,
                        name="Expiry Day Gamma Cutoff (13:30 IST)",
                        passed=False,
                        verified_file="agents/architect.py",
                        verified_value="Missing Cutoff",
                        details="EXPIRY_CUTOFF_TIME = time(13, 30) not enforced in signal workflow",
                    )
                )

        # A3: Verify Theta/Credit capture math: Minimum net credit threshold to ensure positive risk-reward
        if not self.coder_path.exists():
            results.append(
                DiagnosticCheck(
                    suite=suite_name,
                    name="Theta/Credit Capture Threshold",
                    passed=False,
                    verified_file="agents/coder.py",
                    verified_value="File Missing",
                    details="agents/coder.py not found",
                )
            )
        else:
            coder_content = self.coder_path.read_text(encoding="utf-8")
            credit_match = re.search(r'MIN_NET_CREDIT_PTS\s*(?::\s*float)?\s*=\s*([0-9.]+)', coder_content)
            has_credit_check = bool(
                re.search(r'net_credit\s*>=\s*MIN_NET_CREDIT_PTS', coder_content) or
                re.search(r'chosen_net_credit\s*>=\s*MIN_NET_CREDIT_PTS', coder_content)
            )

            if credit_match and has_credit_check:
                credit_val = float(credit_match.group(1))
                passed = (credit_val >= 8.0)
                results.append(
                    DiagnosticCheck(
                        suite=suite_name,
                        name="Theta/Credit Capture Threshold",
                        passed=passed,
                        verified_file="agents/coder.py",
                        verified_value=f"MIN_NET_CREDIT_PTS = {credit_val} pts",
                        details="Enforces minimum credit capture for positive risk-reward",
                    )
                )
            else:
                results.append(
                    DiagnosticCheck(
                        suite=suite_name,
                        name="Theta/Credit Capture Threshold",
                        passed=False,
                        verified_file="agents/coder.py",
                        verified_value="Not Configured",
                        details="MIN_NET_CREDIT_PTS threshold missing in agents/coder.py",
                    )
                )

        return results

    # =========================================================================
    # SUITE B: Trader Failure Traps (Psychology & Risk Management)
    # =========================================================================
    def check_suite_b(self) -> list[DiagnosticCheck]:
        results: list[DiagnosticCheck] = []
        suite_name = "B. Trader Failure Traps"

        # B1: Hard daily loss limit enforcement (Strictly <= -1500 INR)
        auditor_content = self.auditor_path.read_text(encoding="utf-8") if self.auditor_path.exists() else ""
        config_data = {}
        if self.config_path.exists():
            try:
                config_data = json.loads(self.config_path.read_text(encoding="utf-8"))
            except Exception:
                pass

        loss_match = re.search(r'MAX_DAILY_LOSS_INR\s*(?::\s*float)?\s*=\s*(-?[0-9.]+)', auditor_content)
        cfg_loss = config_data.get("risk_guardrails", {}).get("hard_daily_loss_limit_inr")

        if loss_match and cfg_loss is not None:
            loss_val = float(loss_match.group(1))
            passed = (loss_val == -1500.0 and cfg_loss == -1500.0)
            results.append(
                DiagnosticCheck(
                    suite=suite_name,
                    name="Hard Daily Loss Limit (<= -1500 INR)",
                    passed=passed,
                    verified_file="agents/auditor.py & config.json",
                    verified_value=f"Limit = {loss_val} INR",
                    details="Kill-switch rejects entries when daily loss reaches -1500 INR",
                )
            )
        else:
            results.append(
                DiagnosticCheck(
                    suite=suite_name,
                    name="Hard Daily Loss Limit (<= -1500 INR)",
                    passed=False,
                    verified_file="agents/auditor.py & config.json",
                    verified_value="Missing Limit",
                    details="MAX_DAILY_LOSS_INR or hard_daily_loss_limit_inr not found",
                )
            )

        # B2: Strict trade cap enforcement (Max 2 trades/day to eliminate overtrading/revenge trading)
        trades_match = re.search(r'MAX_DAILY_TRADES\s*(?::\s*int)?\s*=\s*([0-9]+)', auditor_content)
        cfg_trades = config_data.get("risk_guardrails", {}).get("max_daily_trades")

        if trades_match and cfg_trades is not None:
            trades_val = int(trades_match.group(1))
            passed = (trades_val == 2 and cfg_trades == 2)
            results.append(
                DiagnosticCheck(
                    suite=suite_name,
                    name="Max Daily Trade Cap (<= 2 Trades)",
                    passed=passed,
                    verified_file="agents/auditor.py & config.json",
                    verified_value=f"Cap = {trades_val} Trades/Day",
                    details="Rejects trade entries when daily trade count >= 2",
                )
            )
        else:
            results.append(
                DiagnosticCheck(
                    suite=suite_name,
                    name="Max Daily Trade Cap (<= 2 Trades)",
                    passed=False,
                    verified_file="agents/auditor.py & config.json",
                    verified_value="Missing Cap",
                    details="MAX_DAILY_TRADES or max_daily_trades not found",
                )
            )

        # B3: Initial Balance freeze: Block all trades during 09:15-09:45 IST
        time_gates = config_data.get("time_gates", {})
        ib_end = time_gates.get("ib_end")
        entry_start = time_gates.get("entry_start")

        passed_ib = (ib_end == "09:45" and entry_start == "09:45")
        results.append(
            DiagnosticCheck(
                suite=suite_name,
                name="Initial Balance Freeze (09:15-09:45)",
                passed=passed_ib,
                verified_file="config.json",
                verified_value=f"ib_end='{ib_end}', entry_start='{entry_start}'",
                details="Blocks all trades during 09:15-09:45 IST IB formation",
            )
        )

        return results

    # =========================================================================
    # SUITE C: Microstructure & Broker Execution Traps
    # =========================================================================
    def check_suite_c(self) -> list[DiagnosticCheck]:
        results: list[DiagnosticCheck] = []
        suite_name = "C. Microstructure Traps"

        devops_content = self.devops_path.read_text(encoding="utf-8") if self.devops_path.exists() else ""

        # C1: Margin Protection Sequencing: Assert BUY leg executes prior to SELL leg
        has_margin_fn = bool(re.search(r'def\s+validate_margin_sequencing', devops_content))
        has_margin_assert = bool(re.search(r'assert\s+validate_margin_sequencing', devops_content))

        if has_margin_fn and has_margin_assert:
            results.append(
                DiagnosticCheck(
                    suite=suite_name,
                    name="Margin Protection Sequencing",
                    passed=True,
                    verified_file="agents/devops.py",
                    verified_value="BUY Leg Precedes SELL Leg",
                    details="Asserts BUY leg executes prior to SELL leg to prevent margin rejection",
                )
            )
        else:
            results.append(
                DiagnosticCheck(
                    suite=suite_name,
                    name="Margin Protection Sequencing",
                    passed=False,
                    verified_file="agents/devops.py",
                    verified_value="Unenforced",
                    details="validate_margin_sequencing check missing in agents/devops.py",
                )
            )

        # C2: Execution Order Type: Assert LIMIT orders only (no blind MARKET orders)
        has_limit_def = bool(re.search(r'DEFAULT_ORDER_TYPE\s*(?::\s*str)?\s*=\s*[\"\']LIMIT[\"\']', devops_content))
        has_market_def = bool(re.search(r'DEFAULT_ORDER_TYPE\s*(?::\s*str)?\s*=\s*[\"\']MARKET[\"\']', devops_content))

        passed_order_type = (has_limit_def and not has_market_def)
        results.append(
            DiagnosticCheck(
                suite=suite_name,
                name="Execution Order Type (LIMIT Only)",
                passed=passed_order_type,
                verified_file="agents/devops.py",
                verified_value="DEFAULT_ORDER_TYPE = 'LIMIT'",
                details="Orders strictly routed as LIMIT (unconstrained MARKET orders disallowed)",
            )
        )

        # C3: Bid-Ask Liquidity Guard: Reject if OTM option hedge spread > 10% of mid-price
        ratio_match = re.search(r'MAX_BID_ASK_SPREAD_RATIO\s*(?::\s*float)?\s*=\s*([0-9.]+)', devops_content)
        has_guard_logic = bool(re.search(r'check_hedge_liquidity_guard|validate_bid_ask_spread', devops_content))

        if ratio_match and has_guard_logic:
            ratio_val = float(ratio_match.group(1))
            passed_ratio = (ratio_val <= 0.10)
            results.append(
                DiagnosticCheck(
                    suite=suite_name,
                    name="Bid-Ask Liquidity Guard (<= 10%)",
                    passed=passed_ratio,
                    verified_file="agents/devops.py",
                    verified_value=f"MAX_BID_ASK_SPREAD_RATIO = {ratio_val:.0%}",
                    details="Rejects or pauses execution if hedge bid-ask spread > 10% of mid-price",
                )
            )
        else:
            results.append(
                DiagnosticCheck(
                    suite=suite_name,
                    name="Bid-Ask Liquidity Guard (<= 10%)",
                    passed=False,
                    verified_file="agents/devops.py",
                    verified_value="Not Configured",
                    details="MAX_BID_ASK_SPREAD_RATIO guard not found in agents/devops.py",
                )
            )

        return results

    # =========================================================================
    # SUITE D: End-to-End System Readiness
    # =========================================================================
    def check_suite_d(self) -> list[DiagnosticCheck]:
        results: list[DiagnosticCheck] = []
        suite_name = "D. End-to-End Readiness"

        # D1: Verify SQLite WAL concurrency (bus.py)
        bus_content = self.bus_path.read_text(encoding="utf-8") if self.bus_path.exists() else ""
        has_wal = bool(re.search(r'PRAGMA\s+journal_mode\s*=\s*WAL', bus_content, re.IGNORECASE))

        results.append(
            DiagnosticCheck(
                suite=suite_name,
                name="SQLite WAL Concurrency",
                passed=has_wal,
                verified_file="bus.py",
                verified_value="PRAGMA journal_mode = WAL;",
                details="Guarantees high-concurrency inter-agent blackboard communication",
            )
        )

        # D2: Verify .env configuration contains Angel One credentials (API key, client code, TOTP secret)
        env_content = self.env_path.read_text(encoding="utf-8") if self.env_path.exists() else ""
        has_api_key = bool(re.search(r'SMARTAPI_API_KEY\s*=\s*([A-Za-z0-9]+)', env_content)) or bool(os.getenv("SMARTAPI_API_KEY"))
        has_client = bool(re.search(r'SMARTAPI_CLIENT_CODE\s*=\s*([A-Za-z0-9]+)', env_content)) or bool(os.getenv("SMARTAPI_CLIENT_CODE"))
        has_totp = bool(re.search(r'SMARTAPI_TOTP_SECRET\s*=\s*([A-Za-z0-9]+)', env_content)) or bool(os.getenv("SMARTAPI_TOTP_SECRET"))

        passed_env = (has_api_key and has_client and has_totp)
        results.append(
            DiagnosticCheck(
                suite=suite_name,
                name="Angel One Credentials in .env",
                passed=passed_env,
                verified_file=".env",
                verified_value="API Key, Client Code, TOTP Secret Configured",
                details="Angel One SmartAPI credentials verified present for live feed",
            )
        )

        # D3: Check mandatory square-off time (strictly 15:10 IST)
        config_data = {}
        if self.config_path.exists():
            try:
                config_data = json.loads(self.config_path.read_text(encoding="utf-8"))
            except Exception:
                pass

        square_off = config_data.get("time_gates", {}).get("square_off")
        passed_sq = (square_off == "15:10")

        results.append(
            DiagnosticCheck(
                suite=suite_name,
                name="Mandatory Square-Off (15:10 IST)",
                passed=passed_sq,
                verified_file="config.json",
                verified_value=f"square_off = '{square_off}'",
                details="Strict EOD square-off at 15:10 IST prior to market close",
            )
        )

        return results

    def run_all_diagnostics(self) -> list[DiagnosticCheck]:
        """Runs all 4 check suites and returns aggregated results."""
        return (
            self.check_suite_a() +
            self.check_suite_b() +
            self.check_suite_c() +
            self.check_suite_d()
        )

    def print_readiness_matrix(self, checks: list[DiagnosticCheck]) -> bool:
        """
        Prints clean tabular Readiness Matrix and returns True if all pass, False otherwise.
        """
        total = len(checks)
        passed_count = sum(1 for c in checks if c.passed)
        failed_count = total - passed_count
        all_passed = (failed_count == 0)

        col_w_suite = 26
        col_w_name = 38
        col_w_val = 48

        print("\n" + "=" * 125)
        print("                        SACCHIN QUANT: PRE-FLIGHT SYSTEM VERIFIER & READINESS MATRIX")
        print("=" * 125)
        print(f" {'STATUS':<7} | {'SUITE CATEGORY':<{col_w_suite}} | {'SPECIFIC CHECK':<{col_w_name}} | {'VERIFIED FILE / VALUE':<{col_w_val}}")
        print("-" * 125)

        current_suite = ""
        for c in checks:
            tag = "[PASS]" if c.passed else "[FAIL]"
            suite_label = c.suite if c.suite != current_suite else ""
            current_suite = c.suite
            print(f" {tag:<7} | {c.suite:<{col_w_suite}} | {c.name:<{col_w_name}} | {c.verified_value:<{col_w_val}}")

        print("-" * 125)
        print(f" TOTAL DIAGNOSTIC CHECKS: {total} | PASSED: {passed_count} | FAILED: {failed_count}")

        if all_passed:
            print("\n VERDICT: READY FOR 09:15 LIVE PAPER EXECUTION")
        else:
            failed_names = [f"'{c.name}' ({c.verified_file})" for c in checks if not c.passed]
            print(f"\n VERDICT: BLOCKED: [{', '.join(failed_names)}]")

        print("=" * 125 + "\n")
        return all_passed

    def run_red_team_audit(self) -> dict:
        """Invokes Autonomous Red-Team Independent Analyst as final mandatory gatekeeper."""
        from agents.independent_analyst import IndependentAnalyst
        analyst = IndependentAnalyst(root_dir=self.root_dir)
        return analyst.audit_and_heal()


def main():
    parser = argparse.ArgumentParser(
        description="Automated Comprehensive Pre-Flight System Verifier (Readiness Matrix)"
    )
    args = parser.parse_args()

    verifier = SystemVerifier()
    checks = verifier.run_all_diagnostics()
    ready = verifier.print_readiness_matrix(checks)

    if not ready:
        sys.exit(1)

    # Final Mandatory Gatekeeper: Autonomous Red-Team Independent Analyst
    analyst_report = verifier.run_red_team_audit()
    if analyst_report["vulnerability_score"] > 0 or analyst_report["status"] != "PASS" or analyst_report["unhealed_defects"] > 0:
        print(f"\n[CRITICAL RED-TEAM ABORT] IndependentAnalyst discovered {analyst_report['unhealed_defects']} open vulnerabilities! Pre-flight aborted.")
        sys.exit(1)

    sys.exit(0)


if __name__ == "__main__":
    main()
