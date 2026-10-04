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
    file_path: Optional[str] = None
    line_number: Optional[int] = None


@dataclass
class Milestone:
    id: int
    name: str
    description: str
    target_component: str
    completed: bool
    evaluation_details: str
    actionable_prompt: str


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
                file_path=str(self.coder_path),
            )

        try:
            tree = ast.parse(self.coder_path.read_text(encoding="utf-8"))
            found_val = None
            found_line = None

            for node in ast.walk(tree):
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Name) and target.id == "MAX_PERMITTED_SPREAD_RISK_INR":
                            found_val = ast.literal_eval(node.value)
                            found_line = node.lineno
                            break

            if found_val is None:
                return AuditResult(
                    name="coder.py: MAX_PERMITTED_SPREAD_RISK_INR",
                    passed=False,
                    message="Constant MAX_PERMITTED_SPREAD_RISK_INR not found in agents/coder.py",
                    file_path=str(self.coder_path),
                )

            if isinstance(found_val, (int, float)) and found_val <= 1500.0:
                return AuditResult(
                    name="coder.py: MAX_PERMITTED_SPREAD_RISK_INR",
                    passed=True,
                    message=f"Value is {found_val} INR (<= 1500.0 INR ceiling)",
                    file_path=str(self.coder_path),
                    line_number=found_line,
                )
            else:
                return AuditResult(
                    name="coder.py: MAX_PERMITTED_SPREAD_RISK_INR",
                    passed=False,
                    message=f"Value {found_val} INR exceeds maximum ceiling of 1500.0 INR",
                    file_path=str(self.coder_path),
                    line_number=found_line,
                )
        except Exception as e:
            return AuditResult(
                name="coder.py: MAX_PERMITTED_SPREAD_RISK_INR",
                passed=False,
                message=f"AST parse error: {e}",
                file_path=str(self.coder_path),
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
                file_path=str(self.coder_path),
            )

        try:
            tree = ast.parse(self.coder_path.read_text(encoding="utf-8"))
            legs_lists: list[tuple[int, list[str]]] = []

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
                                legs_lists.append((node.lineno, actions))

            if not legs_lists:
                return AuditResult(
                    name="coder.py: BUY leg precedes SELL leg",
                    passed=False,
                    message="No 'legs' definitions found in agents/coder.py",
                    file_path=str(self.coder_path),
                )

            for i, (lineno, actions) in enumerate(legs_lists):
                if len(actions) < 2:
                    return AuditResult(
                        name="coder.py: BUY leg precedes SELL leg",
                        passed=False,
                        message=f"Spread structure #{i+1} has fewer than 2 legs: {actions}",
                        file_path=str(self.coder_path),
                        line_number=lineno,
                    )
                if actions[0] != "BUY" or actions[1] != "SELL":
                    return AuditResult(
                        name="coder.py: BUY leg precedes SELL leg",
                        passed=False,
                        message=f"Spread structure #{i+1} violates margin order: {actions} (expected ['BUY', 'SELL'])",
                        file_path=str(self.coder_path),
                        line_number=lineno,
                    )

            first_line = legs_lists[0][0] if legs_lists else None
            return AuditResult(
                name="coder.py: BUY leg precedes SELL leg",
                passed=True,
                message=f"Verified {len(legs_lists)} spread structures: BUY leg precedes SELL leg (Margin Protected)",
                file_path=str(self.coder_path),
                line_number=first_line,
            )
        except Exception as e:
            return AuditResult(
                name="coder.py: BUY leg precedes SELL leg",
                passed=False,
                message=f"Inspection error: {e}",
                file_path=str(self.coder_path),
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
                file_path=str(self.bus_path),
            )

        try:
            content = self.bus_path.read_text(encoding="utf-8")
            line_no = None
            for idx, line in enumerate(content.splitlines(), 1):
                if re.search(r'PRAGMA\s+journal_mode\s*=\s*WAL', line, re.IGNORECASE):
                    line_no = idx
                    break

            if line_no is not None:
                return AuditResult(
                    name="bus.py: SQLite WAL Mode",
                    passed=True,
                    message="PRAGMA journal_mode = WAL; confirmed configured",
                    file_path=str(self.bus_path),
                    line_number=line_no,
                )
            else:
                return AuditResult(
                    name="bus.py: SQLite WAL Mode",
                    passed=False,
                    message="PRAGMA journal_mode = WAL; not found in bus.py",
                    file_path=str(self.bus_path),
                )
        except Exception as e:
            return AuditResult(
                name="bus.py: SQLite WAL Mode",
                passed=False,
                message=f"File read error: {e}",
                file_path=str(self.bus_path),
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
                file_path=str(self.auditor_path),
            )

        try:
            content = self.auditor_path.read_text(encoding="utf-8")
            tree = ast.parse(content)

            loss_val = None
            found_line = None
            for node in ast.walk(tree):
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Name) and target.id == "MAX_DAILY_LOSS_INR":
                            loss_val = ast.literal_eval(node.value)
                            found_line = node.lineno
                            break
                elif isinstance(node, ast.AnnAssign):
                    if isinstance(node.target, ast.Name) and node.target.id == "MAX_DAILY_LOSS_INR" and node.value:
                        loss_val = ast.literal_eval(node.value)
                        found_line = node.lineno
                        break

            if loss_val is None:
                return AuditResult(
                    name="auditor.py: Hard daily loss limit (<= -1500 INR)",
                    passed=False,
                    message="Constant MAX_DAILY_LOSS_INR not found in agents/auditor.py",
                    file_path=str(self.auditor_path),
                )

            # Invariant check: Loss limit cannot be worse than -1500 INR (e.g. -2000 is breach)
            if not (isinstance(loss_val, (int, float)) and loss_val >= -1500.0 and loss_val <= 0.0):
                return AuditResult(
                    name="auditor.py: Hard daily loss limit (<= -1500 INR)",
                    passed=False,
                    message=f"Configured limit {loss_val} INR breaches hard invariant of -1500.0 INR",
                    file_path=str(self.auditor_path),
                    line_number=found_line,
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
                    file_path=str(self.auditor_path),
                    line_number=found_line,
                )

            return AuditResult(
                name="auditor.py: Hard daily loss limit (<= -1500 INR)",
                passed=True,
                message=f"Enforces rejection when daily PnL <= {loss_val} INR (Kill-Switch Active)",
                file_path=str(self.auditor_path),
                line_number=found_line,
            )
        except Exception as e:
            return AuditResult(
                name="auditor.py: Hard daily loss limit (<= -1500 INR)",
                passed=False,
                message=f"Audit inspection error: {e}",
                file_path=str(self.auditor_path),
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
                file_path=str(self.auditor_path),
            )

        try:
            content = self.auditor_path.read_text(encoding="utf-8")
            tree = ast.parse(content)

            trades_val = None
            found_line = None
            for node in ast.walk(tree):
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Name) and target.id == "MAX_DAILY_TRADES":
                            trades_val = ast.literal_eval(node.value)
                            found_line = node.lineno
                            break
                elif isinstance(node, ast.AnnAssign):
                    if isinstance(node.target, ast.Name) and node.target.id == "MAX_DAILY_TRADES" and node.value:
                        trades_val = ast.literal_eval(node.value)
                        found_line = node.lineno
                        break

            if trades_val is None:
                return AuditResult(
                    name="auditor.py: Maximum daily trades limit (<= 2)",
                    passed=False,
                    message="Constant MAX_DAILY_TRADES not found in agents/auditor.py",
                    file_path=str(self.auditor_path),
                )

            if not (isinstance(trades_val, int) and 1 <= trades_val <= 2):
                return AuditResult(
                    name="auditor.py: Maximum daily trades limit (<= 2)",
                    passed=False,
                    message=f"Configured limit {trades_val} trades exceeds hard cap of 2 trades/day",
                    file_path=str(self.auditor_path),
                    line_number=found_line,
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
                    file_path=str(self.auditor_path),
                    line_number=found_line,
                )

            return AuditResult(
                name="auditor.py: Maximum daily trades limit (<= 2)",
                passed=True,
                message=f"Enforces rejection when daily trade count >= {trades_val} (Daily Cap Guarded)",
                file_path=str(self.auditor_path),
                line_number=found_line,
            )
        except Exception as e:
            return AuditResult(
                name="auditor.py: Maximum daily trades limit (<= 2)",
                passed=False,
                message=f"Audit inspection error: {e}",
                file_path=str(self.auditor_path),
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
                file_path=str(self.config_path),
            )

        try:
            content = self.config_path.read_text(encoding="utf-8")
            data = json.loads(content)
            time_gates = data.get("time_gates", {})

            entry_end = time_gates.get("entry_end")
            square_off = time_gates.get("square_off")

            line_no = None
            for idx, line in enumerate(content.splitlines(), 1):
                if "time_gates" in line:
                    line_no = idx
                    break

            if entry_end != "15:05":
                return AuditResult(
                    name="config.json: Mandatory Time-Gates",
                    passed=False,
                    message=f"time_gates.entry_end is '{entry_end}', expected strictly '15:05'",
                    file_path=str(self.config_path),
                    line_number=line_no,
                )

            if square_off != "15:10":
                return AuditResult(
                    name="config.json: Mandatory Time-Gates",
                    passed=False,
                    message=f"time_gates.square_off is '{square_off}', expected strictly '15:10'",
                    file_path=str(self.config_path),
                    line_number=line_no,
                )

            return AuditResult(
                name="config.json: Mandatory Time-Gates",
                passed=True,
                message="entry_end='15:05' & square_off='15:10' strictly configured",
                file_path=str(self.config_path),
                line_number=line_no,
            )
        except Exception as e:
            return AuditResult(
                name="config.json: Mandatory Time-Gates",
                passed=False,
                message=f"JSON parse error: {e}",
                file_path=str(self.config_path),
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
                file_path=str(self.architect_path),
            )

        try:
            content = self.architect_path.read_text(encoding="utf-8")
            lines = content.splitlines()

            cutoff_line = None
            for idx, line in enumerate(lines, 1):
                if "EXPIRY_CUTOFF_TIME" in line or "13:30" in line:
                    cutoff_line = idx
                    break

            has_cutoff_const = bool(
                re.search(r'EXPIRY_CUTOFF_TIME\s*(?::\s*time)?\s*=\s*time\(\s*13\s*,\s*30\s*\)', content)
            )
            has_expiry_gate = bool(
                re.search(r'is_expiry_day\s*\(.*?\)\s*and\s*.*?EXPIRY_CUTOFF_TIME', content) or
                re.search(r'is_expiry_day\s*\(.*?\)\s*and\s*.*?13\s*,\s*30', content)
            )

            if not has_cutoff_const:
                return AuditResult(
                    name="architect.py: Expiry Gamma Cutoff (13:30 IST)",
                    passed=False,
                    message="Constant EXPIRY_CUTOFF_TIME = time(13, 30) not defined in agents/architect.py",
                    file_path=str(self.architect_path),
                    line_number=cutoff_line,
                )

            if not has_expiry_gate:
                return AuditResult(
                    name="architect.py: Expiry Gamma Cutoff (13:30 IST)",
                    passed=False,
                    message="Expiry day 13:30 cutoff condition not enforced in signal workflow",
                    file_path=str(self.architect_path),
                    line_number=cutoff_line,
                )

            return AuditResult(
                name="architect.py: Expiry Gamma Cutoff (13:30 IST)",
                passed=True,
                message="Signals strictly blocked after 13:30 IST on expiry days (Gamma Risk Mitigated)",
                file_path=str(self.architect_path),
                line_number=cutoff_line,
            )
        except Exception as e:
            return AuditResult(
                name="architect.py: Expiry Gamma Cutoff (13:30 IST)",
                passed=False,
                message=f"Inspection error: {e}",
                file_path=str(self.architect_path),
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
                file_path=str(self.devops_path),
            )

        try:
            content = self.devops_path.read_text(encoding="utf-8")
            line_no = None
            for idx, line in enumerate(content.splitlines(), 1):
                if "DEFAULT_ORDER_TYPE" in line or "ordertype" in line:
                    line_no = idx
                    break

            has_limit_def = bool(
                re.search(r'DEFAULT_ORDER_TYPE\s*(?::\s*str)?\s*=\s*[\"\']LIMIT[\"\']', content) or
                re.search(r'[\"\']ordertype[\"\']\s*:\s*[\"\']LIMIT[\"\']', content)
            )
            has_market_routing = bool(
                re.search(r'DEFAULT_ORDER_TYPE\s*(?::\s*str)?\s*=\s*[\"\']MARKET[\"\']', content) or
                re.search(r'[\"\']ordertype[\"\']\s*:\s*[\"\']MARKET[\"\']', content)
            )

            if not has_limit_def:
                return AuditResult(
                    name="devops.py: Limit Order Execution Safety",
                    passed=False,
                    message="LIMIT order type invariant not found in agents/devops.py",
                    file_path=str(self.devops_path),
                    line_number=line_no,
                )

            if has_market_routing:
                return AuditResult(
                    name="devops.py: Limit Order Execution Safety",
                    passed=False,
                    message="Insecure MARKET order routing detected in agents/devops.py",
                    file_path=str(self.devops_path),
                    line_number=line_no,
                )

            return AuditResult(
                name="devops.py: Limit Order Execution Safety",
                passed=True,
                message="Orders strictly routed as LIMIT (unconstrained MARKET orders disallowed)",
                file_path=str(self.devops_path),
                line_number=line_no,
            )
        except Exception as e:
            return AuditResult(
                name="devops.py: Limit Order Execution Safety",
                passed=False,
                message=f"Inspection error: {e}",
                file_path=str(self.devops_path),
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
                file_path=str(self.devops_path),
            )

        try:
            content = self.devops_path.read_text(encoding="utf-8")
            tree = ast.parse(content)

            ratio_val = None
            found_line = None
            for node in ast.walk(tree):
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Name) and target.id == "MAX_BID_ASK_SPREAD_RATIO":
                            ratio_val = ast.literal_eval(node.value)
                            found_line = node.lineno
                            break
                elif isinstance(node, ast.AnnAssign):
                    if isinstance(node.target, ast.Name) and node.target.id == "MAX_BID_ASK_SPREAD_RATIO" and node.value:
                        ratio_val = ast.literal_eval(node.value)
                        found_line = node.lineno
                        break

            if ratio_val is None:
                return AuditResult(
                    name="devops.py: Bid-Ask Spread Liquidity Guard",
                    passed=False,
                    message="Constant MAX_BID_ASK_SPREAD_RATIO not found in agents/devops.py",
                    file_path=str(self.devops_path),
                )

            if not (isinstance(ratio_val, (int, float)) and 0.0 < ratio_val <= 0.10):
                return AuditResult(
                    name="devops.py: Bid-Ask Spread Liquidity Guard",
                    passed=False,
                    message=f"MAX_BID_ASK_SPREAD_RATIO is {ratio_val} (must be <= 0.10 / 10%)",
                    file_path=str(self.devops_path),
                    line_number=found_line,
                )

            has_guard_logic = bool(
                re.search(r'(?:check_hedge_liquidity_guard|validate_bid_ask_spread)', content) and
                re.search(r'mid_price', content)
            )

            if not has_guard_logic:
                return AuditResult(
                    name="devops.py: Bid-Ask Spread Liquidity Guard",
                    passed=False,
                    message="Bid-ask spread validation guard logic not implemented in dispatch flow",
                    file_path=str(self.devops_path),
                    line_number=found_line,
                )

            return AuditResult(
                name="devops.py: Bid-Ask Spread Liquidity Guard",
                passed=True,
                message=f"Execution guarded: rejects if hedge spread > {ratio_val:.0%} of mid-price",
                file_path=str(self.devops_path),
                line_number=found_line,
            )
        except Exception as e:
            return AuditResult(
                name="devops.py: Bid-Ask Spread Liquidity Guard",
                passed=False,
                message=f"Inspection error: {e}",
                file_path=str(self.devops_path),
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

    def evaluate_roadmap_milestones(self) -> list[Milestone]:
        """
        Evaluates system readiness against the 3 roadmap milestones:
        Milestone 1: End-to-end integration test (Architect -> Coder -> Auditor -> DevOps via bus.py)
        Milestone 2: Paper trading replay engine with historical 1-minute tick data.
        Milestone 3: Daily EOD PnL reconciliation & expectancy calculator in audit_logger.py.
        """
        milestones: list[Milestone] = []

        # Milestone 1: Integration Test
        m1_file = self.root_dir / "test_blackboard_integration.py"
        m1_alt_file = self.root_dir / "tests" / "test_blackboard_integration.py"
        m1_completed = False
        m1_details = ""
        m1_target = "test_blackboard_integration.py"
        for candidate in (m1_file, m1_alt_file):
            if candidate.exists():
                content = candidate.read_text(encoding="utf-8")
                if "test_blackboard_end_to_end_flow" in content and "SystemBus" in content:
                    m1_completed = True
                    m1_details = f"Verified end-to-end multi-agent integration test present in {candidate.name}"
                    m1_target = candidate.name
                    break
        if not m1_completed:
            m1_details = "test_blackboard_integration.py not found or missing test_blackboard_end_to_end_flow"

        m1_prompt = (
            "Task: Implement End-to-End Integration Test for Multi-Agent System (Milestone 1)\n\n"
            "Context:\n"
            "Implement an end-to-end integration test (`test_blackboard_integration.py`) verifying the complete lifecycle:\n"
            "Architect (SIGNAL_DETECTED) -> Coder (ORDER_PROPOSED) -> Auditor (ORDER_APPROVED) -> DevOps (ORDER_EXECUTED) via SystemBus (`bus.py`).\n\n"
            "Requirements:\n"
            "1. Connect all 4 agents using a temporary SQLite `bus.db`.\n"
            "2. Verify status transitions (PENDING -> PROCESSING -> COMPLETED) and risk validations.\n"
            "3. Assert journal logging in `audit_logger.py`.\n"
            "4. Verify test passes with pytest."
        )

        milestones.append(
            Milestone(
                id=1,
                name="End-to-end integration test (Architect -> Coder -> Auditor -> DevOps via bus.py)",
                description="Blackboard pattern integration test verifying complete multi-agent event cycle.",
                target_component=m1_target,
                completed=m1_completed,
                evaluation_details=m1_details,
                actionable_prompt=m1_prompt,
            )
        )

        # Milestone 2: Paper trading replay engine
        m2_file = self.root_dir / "replay_engine.py"
        m2_alt_file = self.root_dir / "historical_replay.py"
        m2_completed = False
        m2_details = ""
        m2_target = "replay_engine.py"
        for candidate in (m2_file, m2_alt_file):
            if candidate.exists():
                content = candidate.read_text(encoding="utf-8")
                if "ReplayEngine" in content or "replay" in content.lower():
                    m2_completed = True
                    m2_details = f"Replay engine found in {candidate.name}"
                    m2_target = candidate.name
                    break
        if not m2_completed:
            m2_details = "replay_engine.py not found; historical 1-minute tick replay engine is uncompleted."

        m2_prompt = (
            "Task: Implement Paper Trading Replay Engine with Historical 1-Minute Tick Data (Milestone 2)\n\n"
            "Context:\n"
            "All system invariants and Milestone 1 (End-to-end integration test) are satisfied. "
            "The next development milestone is creating an event-driven paper trading replay engine (`replay_engine.py`) "
            "that feeds historical 1-minute OHLCV/tick candles through the SystemBus.\n\n"
            "Requirements:\n"
            "1. Create `replay_engine.py`:\n"
            "   - Implement `ReplayEngine` class capable of loading historical 1-minute NIFTY candles/ticks.\n"
            "   - Step through candles chronologically in simulated IST time.\n"
            "   - Publish market data events onto SystemBus (`TICK_UPDATE` / `CANDLE_CLOSE`) to drive ArchitectAgent.\n"
            "   - Simulate execution with realistic fill slippage against simulated order book.\n"
            "2. Integrate with `agents/devops.py` paper-trading mode and `RiskGuard`.\n"
            "3. Add unit & replay tests in `tests/test_replay_engine.py` verifying deterministic chronological execution.\n"
            "4. Run `pytest tests/test_replay_engine.py` to confirm all tests pass."
        )

        milestones.append(
            Milestone(
                id=2,
                name="Paper trading replay engine with historical 1-minute tick data",
                description="Simulated historical market replay engine feeding 1-minute candle events through SystemBus.",
                target_component=m2_target,
                completed=m2_completed,
                evaluation_details=m2_details,
                actionable_prompt=m2_prompt,
            )
        )

        # Milestone 3: Daily EOD PnL reconciliation & expectancy calculator in audit_logger.py
        m3_file = self.root_dir / "audit_logger.py"
        m3_completed = False
        m3_details = ""
        m3_target = "audit_logger.py"
        if m3_file.exists():
            content = m3_file.read_text(encoding="utf-8")
            has_expectancy = bool(re.search(r'def\s+calculate_expectancy', content))
            has_eod_rec = bool(re.search(r'def\s+(?:reconcile_eod|reconcile_daily_pnl)', content))
            if has_expectancy and has_eod_rec:
                m3_completed = True
                m3_details = "AuditLogger implements calculate_expectancy() and reconcile_eod()."
            else:
                missing = []
                if not has_expectancy:
                    missing.append("calculate_expectancy")
                if not has_eod_rec:
                    missing.append("reconcile_eod")
                m3_details = f"audit_logger.py missing methods: {', '.join(missing)}"
        else:
            m3_details = "audit_logger.py not found"

        m3_prompt = (
            "Task: Daily EOD PnL Reconciliation & Expectancy Calculator in audit_logger.py (Milestone 3)\n\n"
            "Context:\n"
            "Milestone 1 (Integration Tests) and Milestone 2 (Replay Engine) are complete. "
            "The next milestone is implementing daily EOD PnL reconciliation and mathematical expectancy tracking in `audit_logger.py`.\n\n"
            "Requirements:\n"
            "1. In `audit_logger.py`, implement `calculate_expectancy()`:\n"
            "   - Expectancy formula: (Win Rate * Avg Win) - (Loss Rate * Avg Loss).\n"
            "   - Return Profit Factor, Win/Loss Ratio, and Edge Expectancy (in INR).\n"
            "2. In `audit_logger.py`, implement `reconcile_eod(date_str, broker_pnl)`:\n"
            "   - Reconcile journaled realized PnL against clearing house / broker settlement.\n"
            "   - Flag slippage anomalies and un-reconciled position states.\n"
            "3. Add unit tests in `tests/test_audit_logger.py` asserting mathematical accuracy and edge cases.\n"
            "4. Run `pytest tests/test_audit_logger.py`."
        )

        milestones.append(
            Milestone(
                id=3,
                name="Daily EOD PnL reconciliation & expectancy calculator in audit_logger.py",
                description="ACID EOD trade reconciliation and quantitative mathematical expectancy calculation.",
                target_component=m3_target,
                completed=m3_completed,
                evaluation_details=m3_details,
                actionable_prompt=m3_prompt,
            )
        )

        return milestones

    def get_next_uncompleted_milestone(self) -> Optional[Milestone]:
        """Returns the first uncompleted milestone in chronological order."""
        for m in self.evaluate_roadmap_milestones():
            if not m.completed:
                return m
        return None

    def generate_remediation_prompt(self, failed_results: list[AuditResult]) -> str:
        """
        Generates a prioritized remediation prompt specifying the exact file and lines to fix.
        """
        lines = [
            "=" * 80,
            "         OVERSEER AGENT: INVARIANT AUDIT FAILED - REMEDIATION REQUIRED",
            "=" * 80,
            f"Detected {len(failed_results)} static invariant violation(s) blocking development progression.\n",
            "PRIORITIZED REMEDIATION PROMPT:",
            "-" * 80,
        ]

        for i, r in enumerate(failed_results, 1):
            file_str = r.file_path or "Unknown File"
            line_str = f"Line {r.line_number}" if r.line_number else "Global/File scope"
            lines.append(f"{i}. Invariant: {r.name}")
            lines.append(f"   Target File: {file_str}")
            lines.append(f"   Location:    {line_str}")
            lines.append(f"   Violation:   {r.message}")
            lines.append(f"   Fix Action:  Resolve invariant violation in `{Path(file_str).name}` at {line_str}.\n")

        lines.extend([
            "-" * 80,
            "Instruction: Apply the fixes above and re-run `python agents/overseer.py --next` to resume roadmap progression.",
            "=" * 80,
        ])
        return "\n".join(lines)

    def generate_roadmap_prompt(self) -> str:
        """
        Generates the ready-to-run prompt for the very next uncompleted roadmap milestone.
        """
        milestones = self.evaluate_roadmap_milestones()
        next_m = self.get_next_uncompleted_milestone()

        lines = [
            "=" * 80,
            "           AUTONOMOUS TASK GENERATOR: NEXT DEVELOPMENT ROADMAP TASK",
            "=" * 80,
            "ALL SYSTEM INVARIANTS SATISFIED [PASS]\n",
            "CURRENT ROADMAP STATUS:",
        ]

        for m in milestones:
            tag = "[X]" if m.completed else "[ ]"
            lines.append(f"  {tag} Milestone {m.id}: {m.name}")

        lines.append("\n" + "-" * 80)

        if next_m is None:
            lines.extend([
                "ALL ROADMAP MILESTONES COMPLETED!",
                "All systems verified: End-to-end tests, Replay Engine, and EOD Expectancy Calculator are active.",
                "Recommended next step: Run paper trading validation session or live broker dry-run.",
                "-" * 80,
            ])
        else:
            lines.extend([
                f"READY-TO-RUN PROMPT FOR NEXT MILESTONE (Milestone {next_m.id}):",
                "-" * 80,
                next_m.actionable_prompt,
                "-" * 80,
            ])

        lines.append("=" * 80)
        return "\n".join(lines)

    def generate_next_task(self) -> tuple[str, int]:
        """
        Runs internal audit.
        If any invariant FAILS: returns (prioritized_remediation_prompt, 1).
        If all invariants PASS: returns (actionable_roadmap_prompt, 0).
        """
        results = self.run_full_audit()
        failed = [r for r in results if not r.passed]

        if failed:
            prompt = self.generate_remediation_prompt(failed)
            return prompt, 1
        else:
            prompt = self.generate_roadmap_prompt()
            return prompt, 0


def main():
    parser = argparse.ArgumentParser(
        description="Overseer Agent: Static Code Invariant Auditor & Task Generator"
    )
    parser.add_argument(
        "--audit",
        action="store_true",
        help="Run static code invariant verification and display scorecard",
    )
    parser.add_argument(
        "--next",
        action="store_true",
        help="Evaluate system readiness and generate prioritized remediation or next roadmap task prompt",
    )
    args = parser.parse_args()

    auditor = StaticInvariantAuditor()

    if args.next:
        prompt, exit_code = auditor.generate_next_task()
        print(prompt)
        sys.exit(exit_code)
    else:
        results = auditor.run_full_audit()
        success = auditor.print_scorecard(results)
        sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
