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

    def run_full_audit(self) -> list[AuditResult]:
        """Runs all static invariant checks and returns results."""
        return [
            self.audit_coder_max_risk(),
            self.audit_coder_leg_ordering(),
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

        print("\n" + "=" * 70)
        print("           OVERSEER AGENT: STATIC INVARIANT AUDIT SCORECARD")
        print("=" * 70)

        for r in results:
            tag = "[PASS]" if r.passed else "[FAIL]"
            print(f" {tag:<7} | {r.name:<38} | {r.message}")

        print("=" * 70)
        print(f" TOTAL INVARIANTS: {total} | PASSED: {passed_count} | FAILED: {failed_count}")
        status_line = "ALL INVARIANTS SATISFIED [PASS]" if all_passed else "INVARIANT VIOLATIONS DETECTED [FAIL]"
        print(f" STATUS: {status_line}")
        print("=" * 70 + "\n")

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
