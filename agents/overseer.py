"""
Overseer Agent: Static Invariant Auditor
Module: agents/overseer.py

Responsibilities:
1. Static code validation of trading invariants without executing runtime market data.
2. Enforces Coder Agent invariants:
   - MAX_PERMITTED_SPREAD_RISK_INR exists and <= 1500.0 INR.
   - BUY leg precedes SELL leg in option spreads (preventing broker margin rejection).
3. Enforces Bus invariants:
   - SQLite WAL mode (PRAGMA journal_mode = WAL;) for inter-agent concurrency.
4. Terminal Scorecard & CLI interface (`python agents/overseer.py --audit`).
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# Fix Windows console UTF-8 encoding
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


@dataclass
class AuditResult:
    name: str
    passed: bool
    message: str


class StaticInvariantAuditor:
    """
    Performs static AST and syntactic inspection of the quant codebase to guarantee
    mathematical and architectural invariants before live execution.
    """

    def __init__(self, root_dir: Optional[Path] = None):
        self.root_dir = root_dir or Path(__file__).parent.parent
        self.coder_path = self.root_dir / "agents" / "coder.py"
        self.auditor_path = self.root_dir / "agents" / "auditor.py"
        self.architect_path = self.root_dir / "agents" / "architect.py"
        self.devops_path = self.root_dir / "agents" / "devops.py"
        self.config_path = self.root_dir / "config.json"
        self.bus_path = self.root_dir / "bus.py"

    def audit_coder_max_risk(self) -> AuditResult:
        """
        Verifies that MAX_PERMITTED_SPREAD_RISK_INR exists in agents/coder.py
        and is strictly <= 1500.0 INR.
        """
        if not self.coder_path.exists():
            return AuditResult(
                name="coder.py: MAX_PERMITTED_SPREAD_RISK_INR",
                passed=False,
                message=f"File not found: {self.coder_path}",
            )

        try:
            tree = ast.parse(self.coder_path.read_text(encoding="utf-8"))
            found_val = None

            for node in ast.walk(tree):
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Name) and target.id == "MAX_PERMITTED_SPREAD_RISK_INR":
                            found_val = ast.literal_eval(node.value)
                            break

            if found_val is None:
                return AuditResult(
                    name="coder.py: MAX_PERMITTED_SPREAD_RISK_INR",
                    passed=False,
                    message="Constant MAX_PERMITTED_SPREAD_RISK_INR not found in agents/coder.py",
                )

            if isinstance(found_val, (int, float)) and found_val <= 1500.0:
                return AuditResult(
                    name="coder.py: MAX_PERMITTED_SPREAD_RISK_INR",
                    passed=True,
                    message=f"Value is {found_val} INR (<= 1500.0 INR ceiling)",
                )
            else:
                return AuditResult(
                    name="coder.py: MAX_PERMITTED_SPREAD_RISK_INR",
                    passed=False,
                    message=f"Value {found_val} INR exceeds maximum ceiling of 1500.0 INR",
                )
        except Exception as e:
            return AuditResult(
                name="coder.py: MAX_PERMITTED_SPREAD_RISK_INR",
                passed=False,
                message=f"AST parse error: {e}",
            )

    def audit_coder_leg_ordering(self) -> AuditResult:
        """
        Verifies that in synthesized option spread legs in agents/coder.py,
        the BUY leg appears before the SELL leg to prevent broker margin rejection.
        """
        if not self.coder_path.exists():
            return AuditResult(
                name="coder.py: BUY leg precedes SELL leg",
                passed=False,
                message=f"File not found: {self.coder_path}",
            )

        try:
            tree = ast.parse(self.coder_path.read_text(encoding="utf-8"))
            legs_lists: list[list[str]] = []

            for node in ast.walk(tree):
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Name) and target.id == "legs":
                            if isinstance(node.value, ast.List) and len(node.value.elts) >= 2:
                                actions = []
                                for elt in node.value.elts:
                                    if isinstance(elt, ast.Dict):
                                        for k, v in zip(elt.keys, elt.values):
                                            if isinstance(k, ast.Constant) and k.value == "action":
                                                if isinstance(v, ast.Constant):
                                                    actions.append(v.value)
                                legs_lists.append(actions)

            if not legs_lists:
                return AuditResult(
                    name="coder.py: BUY leg precedes SELL leg",
                    passed=False,
                    message="No 'legs' definitions found in agents/coder.py",
                )

            for i, actions in enumerate(legs_lists):
                if len(actions) < 2:
                    return AuditResult(
                        name="coder.py: BUY leg precedes SELL leg",
                        passed=False,
                        message=f"Spread structure #{i+1} has fewer than 2 legs: {actions}",
                    )
                if actions[0] != "BUY" or actions[1] != "SELL":
                    return AuditResult(
                        name="coder.py: BUY leg precedes SELL leg",
                        passed=False,
                        message=f"Spread structure #{i+1} violates margin order: {actions} (expected ['BUY', 'SELL'])",
                    )

            return AuditResult(
                name="coder.py: BUY leg precedes SELL leg",
                passed=True,
                message=f"Verified {len(legs_lists)} spread structures: BUY leg precedes SELL leg (Margin Protected)",
            )
        except Exception as e:
            return AuditResult(
                name="coder.py: BUY leg precedes SELL leg",
                passed=False,
                message=f"Inspection error: {e}",
            )

    def audit_bus_wal_mode(self) -> AuditResult:
        """
        Verifies that bus.py executes PRAGMA journal_mode = WAL; for high concurrency.
        """
        if not self.bus_path.exists():
            return AuditResult(
                name="bus.py: SQLite WAL Mode",
                passed=False,
                message=f"File not found: {self.bus_path}",
            )

        try:
            content = self.bus_path.read_text(encoding="utf-8")
            match = re.search(r'PRAGMA\s+journal_mode\s*=\s*WAL', content, re.IGNORECASE)
            if match:
                return AuditResult(
                    name="bus.py: SQLite WAL Mode",
                    passed=True,
                    message="PRAGMA journal_mode = WAL; confirmed configured",
                )
            else:
                return AuditResult(
                    name="bus.py: SQLite WAL Mode",
                    passed=False,
                    message="PRAGMA journal_mode = WAL; not found in bus.py",
                )
        except Exception as e:
            return AuditResult(
                name="bus.py: SQLite WAL Mode",
                passed=False,
                message=f"File read error: {e}",
            )

    def audit_auditor_daily_loss_limit(self) -> AuditResult:
        """
        Verifies that agents/auditor.py enforces a hard daily loss limit
        rejecting trade entries when daily PnL <= -1500.0 INR.
        """
        if not self.auditor_path.exists():
            return AuditResult(
                name="auditor.py: Hard daily loss limit (<= -1500 INR)",
                passed=False,
                message=f"File not found: {self.auditor_path}",
            )

        try:
            content = self.auditor_path.read_text(encoding="utf-8")
            tree = ast.parse(content)

            loss_val = None
            for node in ast.walk(tree):
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Name) and target.id == "MAX_DAILY_LOSS_INR":
                            loss_val = ast.literal_eval(node.value)
                            break
                elif isinstance(node, ast.AnnAssign):
                    if isinstance(node.target, ast.Name) and node.target.id == "MAX_DAILY_LOSS_INR" and node.value:
                        loss_val = ast.literal_eval(node.value)
                        break

            if loss_val is None:
                return AuditResult(
                    name="auditor.py: Hard daily loss limit (<= -1500 INR)",
                    passed=False,
                    message="Constant MAX_DAILY_LOSS_INR not found in agents/auditor.py",
                )

            # Invariant check: Loss limit cannot be worse than -1500 INR (e.g. -2000 is breach)
            if not (isinstance(loss_val, (int, float)) and loss_val >= -1500.0 and loss_val <= 0.0):
                return AuditResult(
                    name="auditor.py: Hard daily loss limit (<= -1500 INR)",
                    passed=False,
                    message=f"Configured limit {loss_val} INR breaches hard invariant of -1500.0 INR",
                )

            # Check that an invariant condition checks total_pnl <= MAX_DAILY_LOSS_INR or <= -1500
            has_check = bool(
                re.search(r'total_pnl\s*<=\s*(MAX_DAILY_LOSS_INR|-1500)', content)
            )
            if not has_check:
                return AuditResult(
                    name="auditor.py: Hard daily loss limit (<= -1500 INR)",
                    passed=False,
                    message="Invariant check for total_pnl <= MAX_DAILY_LOSS_INR not implemented in audit flow",
                )

            return AuditResult(
                name="auditor.py: Hard daily loss limit (<= -1500 INR)",
                passed=True,
                message=f"Enforces rejection when daily PnL <= {loss_val} INR (Kill-Switch Active)",
            )
        except Exception as e:
            return AuditResult(
                name="auditor.py: Hard daily loss limit (<= -1500 INR)",
                passed=False,
                message=f"Audit inspection error: {e}",
            )

    def audit_auditor_max_daily_trades(self) -> AuditResult:
        """
        Verifies that agents/auditor.py enforces a maximum daily trade count
        rejecting trade entries when daily trade count >= 2.
        """
        if not self.auditor_path.exists():
            return AuditResult(
                name="auditor.py: Maximum daily trades limit (<= 2)",
                passed=False,
                message=f"File not found: {self.auditor_path}",
            )

        try:
            content = self.auditor_path.read_text(encoding="utf-8")
            tree = ast.parse(content)

            trades_val = None
            for node in ast.walk(tree):
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Name) and target.id == "MAX_DAILY_TRADES":
                            trades_val = ast.literal_eval(node.value)
                            break
                elif isinstance(node, ast.AnnAssign):
                    if isinstance(node.target, ast.Name) and node.target.id == "MAX_DAILY_TRADES" and node.value:
                        trades_val = ast.literal_eval(node.value)
                        break

            if trades_val is None:
                return AuditResult(
                    name="auditor.py: Maximum daily trades limit (<= 2)",
                    passed=False,
                    message="Constant MAX_DAILY_TRADES not found in agents/auditor.py",
                )

            if not (isinstance(trades_val, int) and 1 <= trades_val <= 2):
                return AuditResult(
                    name="auditor.py: Maximum daily trades limit (<= 2)",
                    passed=False,
                    message=f"Configured limit {trades_val} trades exceeds hard cap of 2 trades/day",
                )

            # Check that an invariant condition checks trade_count >= MAX_DAILY_TRADES or >= 2
            has_check = bool(
                re.search(r'trade_count\s*>=\s*(MAX_DAILY_TRADES|2)', content)
            )
            if not has_check:
                return AuditResult(
                    name="auditor.py: Maximum daily trades limit (<= 2)",
                    passed=False,
                    message="Invariant check for trade_count >= MAX_DAILY_TRADES not implemented in audit flow",
                )

            return AuditResult(
                name="auditor.py: Maximum daily trades limit (<= 2)",
                passed=True,
                message=f"Enforces rejection when daily trade count >= {trades_val} (Daily Cap Guarded)",
            )
        except Exception as e:
            return AuditResult(
                name="auditor.py: Maximum daily trades limit (<= 2)",
                passed=False,
                message=f"Audit inspection error: {e}",
            )

    def audit_config_time_gates(self) -> AuditResult:
        """
        Verifies that config.json strictly enforces:
        - time_gates.entry_end == "15:05"
        - time_gates.square_off == "15:10"
        """
        if not self.config_path.exists():
            return AuditResult(
                name="config.json: Mandatory Time-Gates",
                passed=False,
                message=f"File not found: {self.config_path}",
            )

        try:
            data = json.loads(self.config_path.read_text(encoding="utf-8"))
            time_gates = data.get("time_gates", {})

            entry_end = time_gates.get("entry_end")
            square_off = time_gates.get("square_off")

            if entry_end != "15:05":
                return AuditResult(
                    name="config.json: Mandatory Time-Gates",
                    passed=False,
                    message=f"time_gates.entry_end is '{entry_end}', expected strictly '15:05'",
                )

            if square_off != "15:10":
                return AuditResult(
                    name="config.json: Mandatory Time-Gates",
                    passed=False,
                    message=f"time_gates.square_off is '{square_off}', expected strictly '15:10'",
                )

            return AuditResult(
                name="config.json: Mandatory Time-Gates",
                passed=True,
                message="entry_end='15:05' & square_off='15:10' strictly configured",
            )
        except Exception as e:
            return AuditResult(
                name="config.json: Mandatory Time-Gates",
                passed=False,
                message=f"JSON parse error: {e}",
            )

    def audit_architect_expiry_gamma_cutoff(self) -> AuditResult:
        """
        Verifies that agents/architect.py restricts new strategy signals after 13:30 IST
        on weekly/monthly expiry days to eliminate high-gamma risk.
        """
        if not self.architect_path.exists():
            return AuditResult(
                name="architect.py: Expiry Gamma Cutoff (13:30 IST)",
                passed=False,
                message=f"File not found: {self.architect_path}",
            )

        try:
            content = self.architect_path.read_text(encoding="utf-8")

            # Check 1: EXPIRY_CUTOFF_TIME constant or 13:30 check
            has_cutoff_const = bool(
                re.search(r'EXPIRY_CUTOFF_TIME\s*(?::\s*time)?\s*=\s*time\(\s*13\s*,\s*30\s*\)', content)
            )
            # Check 2: Check for expiry day gating in on_candle or signal flow
            has_expiry_gate = bool(
                re.search(r'is_expiry_day\s*\(.*?\)\s*and\s*.*?EXPIRY_CUTOFF_TIME', content) or
                re.search(r'is_expiry_day\s*\(.*?\)\s*and\s*.*?13\s*,\s*30', content)
            )

            if not has_cutoff_const:
                return AuditResult(
                    name="architect.py: Expiry Gamma Cutoff (13:30 IST)",
                    passed=False,
                    message="Constant EXPIRY_CUTOFF_TIME = time(13, 30) not defined in agents/architect.py",
                )

            if not has_expiry_gate:
                return AuditResult(
                    name="architect.py: Expiry Gamma Cutoff (13:30 IST)",
                    passed=False,
                    message="Expiry day 13:30 cutoff condition not enforced in signal workflow",
                )

            return AuditResult(
                name="architect.py: Expiry Gamma Cutoff (13:30 IST)",
                passed=True,
                message="Signals strictly blocked after 13:30 IST on expiry days (Gamma Risk Mitigated)",
            )
        except Exception as e:
            return AuditResult(
                name="architect.py: Expiry Gamma Cutoff (13:30 IST)",
                passed=False,
                message=f"Inspection error: {e}",
            )

    def audit_devops_limit_order_safety(self) -> AuditResult:
        """
        Verifies that agents/devops.py routes orders strictly with order_type="LIMIT",
        disallowing unconstrained MARKET orders to prevent fill slippage.
        """
        if not self.devops_path.exists():
            return AuditResult(
                name="devops.py: Limit Order Execution Safety",
                passed=False,
                message=f"File not found: {self.devops_path}",
            )

        try:
            content = self.devops_path.read_text(encoding="utf-8")

            # Check 1: Must define LIMIT as default order type or specify LIMIT in placeOrder
            has_limit_def = bool(
                re.search(r'DEFAULT_ORDER_TYPE\s*(?::\s*str)?\s*=\s*[\"\']LIMIT[\"\']', content) or
                re.search(r'[\"\']ordertype[\"\']\s*:\s*[\"\']LIMIT[\"\']', content)
            )

            # Check 2: Unconstrained MARKET orders must NOT be the default routing
            has_market_routing = bool(
                re.search(r'DEFAULT_ORDER_TYPE\s*(?::\s*str)?\s*=\s*[\"\']MARKET[\"\']', content) or
                re.search(r'[\"\']ordertype[\"\']\s*:\s*[\"\']MARKET[\"\']', content)
            )

            if not has_limit_def:
                return AuditResult(
                    name="devops.py: Limit Order Execution Safety",
                    passed=False,
                    message="LIMIT order type invariant not found in agents/devops.py",
                )

            if has_market_routing:
                return AuditResult(
                    name="devops.py: Limit Order Execution Safety",
                    passed=False,
                    message="Insecure MARKET order routing detected in agents/devops.py",
                )

            return AuditResult(
                name="devops.py: Limit Order Execution Safety",
                passed=True,
                message="Orders strictly routed as LIMIT (unconstrained MARKET orders disallowed)",
            )
        except Exception as e:
            return AuditResult(
                name="devops.py: Limit Order Execution Safety",
                passed=False,
                message=f"Inspection error: {e}",
            )

    def audit_devops_bid_ask_spread_guard(self) -> AuditResult:
        """
        Verifies that agents/devops.py enforces a Bid-Ask spread guard:
        rejects or pauses execution if the hedge leg bid-ask spread exceeds 10% of its mid-price.
        """
        if not self.devops_path.exists():
            return AuditResult(
                name="devops.py: Bid-Ask Spread Liquidity Guard",
                passed=False,
                message=f"File not found: {self.devops_path}",
            )

        try:
            content = self.devops_path.read_text(encoding="utf-8")
            tree = ast.parse(content)

            ratio_val = None
            for node in ast.walk(tree):
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Name) and target.id == "MAX_BID_ASK_SPREAD_RATIO":
                            ratio_val = ast.literal_eval(node.value)
                            break
                elif isinstance(node, ast.AnnAssign):
                    if isinstance(node.target, ast.Name) and node.target.id == "MAX_BID_ASK_SPREAD_RATIO" and node.value:
                        ratio_val = ast.literal_eval(node.value)
                        break

            if ratio_val is None:
                return AuditResult(
                    name="devops.py: Bid-Ask Spread Liquidity Guard",
                    passed=False,
                    message="Constant MAX_BID_ASK_SPREAD_RATIO not found in agents/devops.py",
                )

            if not (isinstance(ratio_val, (int, float)) and 0.0 < ratio_val <= 0.10):
                return AuditResult(
                    name="devops.py: Bid-Ask Spread Liquidity Guard",
                    passed=False,
                    message=f"MAX_BID_ASK_SPREAD_RATIO is {ratio_val} (must be <= 0.10 / 10%)",
                )

            # Check that liquidity check method/function exists and guards execution
            has_guard_logic = bool(
                re.search(r'(?:check_hedge_liquidity_guard|validate_bid_ask_spread)', content) and
                re.search(r'mid_price', content)
            )

            if not has_guard_logic:
                return AuditResult(
                    name="devops.py: Bid-Ask Spread Liquidity Guard",
                    passed=False,
                    message="Bid-ask spread validation guard logic not implemented in dispatch flow",
                )

            return AuditResult(
                name="devops.py: Bid-Ask Spread Liquidity Guard",
                passed=True,
                message=f"Execution guarded: rejects if hedge spread > {ratio_val:.0%} of mid-price",
            )
        except Exception as e:
            return AuditResult(
                name="devops.py: Bid-Ask Spread Liquidity Guard",
                passed=False,
                message=f"Inspection error: {e}",
            )

    def run_full_audit(self) -> list[AuditResult]:
        """Runs all static invariant checks and returns results."""
        return [
            self.audit_coder_max_risk(),
            self.audit_coder_leg_ordering(),
            self.audit_auditor_daily_loss_limit(),
            self.audit_auditor_max_daily_trades(),
            self.audit_config_time_gates(),
            self.audit_architect_expiry_gamma_cutoff(),
            self.audit_devops_limit_order_safety(),
            self.audit_devops_bid_ask_spread_guard(),
            self.audit_bus_wal_mode(),
        ]

    def print_scorecard(self, results: list[AuditResult]) -> bool:
        """
        Prints a clean PASS/FAIL scorecard in the terminal.
        Returns True if all checks passed, False otherwise.
        """
        total = len(results)
        passed_count = sum(1 for r in results if r.passed)
        failed_count = total - passed_count
        all_passed = (failed_count == 0)

        print("\n" + "=" * 80)
        print("             OVERSEER AGENT: STATIC INVARIANT AUDIT SCORECARD")
        print("=" * 80)

        for r in results:
            tag = "[PASS]" if r.passed else "[FAIL]"
            print(f" {tag:<7} | {r.name:<48} | {r.message}")

        print("=" * 80)
        print(f" TOTAL INVARIANTS: {total} | PASSED: {passed_count} | FAILED: {failed_count}")
        status_line = "ALL INVARIANTS SATISFIED [PASS]" if all_passed else "INVARIANT VIOLATIONS DETECTED [FAIL]"
        print(f" STATUS: {status_line}")
        print("=" * 80 + "\n")

        return all_passed


def main():
    parser = argparse.ArgumentParser(
        description="Overseer Agent: Static Code Invariant Auditor"
    )
    parser.add_argument(
        "--audit",
        action="store_true",
        help="Run static code invariant verification",
    )
    args = parser.parse_args()

    auditor = StaticInvariantAuditor()
    results = auditor.run_full_audit()
    success = auditor.print_scorecard(results)

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
