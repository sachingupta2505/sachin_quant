"""
Autonomous File Watcher & Red-Team Code Audit Hook
Module: scripts/watch_and_audit.py

Responsibilities:
1. Monitors workspace python files for code changes in real-time.
2. On detecting any modification, automatically invokes IndependentAnalyst.audit_and_heal().
3. Flags flaws and auto-heals them before deployment.
4. Provides `--once` flag for CI/CD and pre-flight validation.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agents.independent_analyst import IndependentAnalyst

IST = ZoneInfo("Asia/Kolkata")
WATCHED_DIRS = [
    REPO_ROOT / "agents",
    REPO_ROOT / "scripts",
    REPO_ROOT / "tests",
]
WATCHED_FILES = [
    REPO_ROOT / "main_runner.py",
    REPO_ROOT / "execution_engine.py",
    REPO_ROOT / "risk_guard.py",
    REPO_ROOT / "bus.py",
    REPO_ROOT / "audit_logger.py",
    REPO_ROOT / "fee_calculator.py",
    REPO_ROOT / "config.json",
]


def collect_file_mtimes() -> dict[Path, float]:
    """Collects current modification timestamps for all relevant workspace files."""
    mtimes = {}
    for d in WATCHED_DIRS:
        if d.exists():
            for p in d.rglob("*.py"):
                if "__pycache__" not in str(p) and not p.name.startswith("."):
                    mtimes[p] = p.stat().st_mtime

    for f in WATCHED_FILES:
        if f.exists():
            mtimes[f] = f.stat().st_mtime
    return mtimes


def run_single_audit(heal: bool = True) -> int:
    """Executes a single audit and returns exit code (0 = PASS, 1 = FAIL)."""
    analyst = IndependentAnalyst(root_dir=REPO_ROOT)
    report = analyst.audit_and_heal(max_iterations=5 if heal else 1)

    print("\n" + "=" * 95)
    print("        SACCHIN QUANT: RED-TEAM INDEPENDENT AUDIT (WATCH & AUDIT)")
    print("=" * 95)
    print(f" Status:               {report['status']}")
    print(f" Vulnerability Score:  {report['vulnerability_score']} / 100")
    print(f" Iterations Performed: {report['iterations_performed']}")
    print(f" Auto-Healed Defects:  {report['healed_count']}")
    print(f" Unhealed Defects:     {report['unhealed_defects']}")
    print("-" * 95)

    for st in report["stress_tests"]:
        tag = "[PASS]" if st["passed"] else "[FAIL]"
        print(f" {tag:<7} | {st['name']:<45} | {st['details']}")

    print("=" * 95)
    return 0 if report["status"] == "PASS" else 1


def run_watcher(poll_interval: float = 2.0) -> None:
    """Continuously monitors codebase and triggers audit on file change."""
    print("=" * 75)
    print("  SACHIN QUANT: AUTONOMOUS RED-TEAM CODE WATCHER ACTIVATED")
    print(f"  Watching: {[d.name for d in WATCHED_DIRS]} + root engine modules")
    print(f"  Poll interval: {poll_interval}s | Press Ctrl+C to terminate")
    print("=" * 75)

    last_mtimes = collect_file_mtimes()
    # Run initial audit
    run_single_audit(heal=True)

    try:
        while True:
            time.sleep(poll_interval)
            current_mtimes = collect_file_mtimes()

            modified = []
            for path, mtime in current_mtimes.items():
                if path not in last_mtimes or mtime > last_mtimes[path]:
                    modified.append(path)

            if modified:
                rel_names = [p.name for p in modified]
                now_str = datetime.now(IST).strftime("%H:%M:%S")
                print(f"\n[{now_str} IST] Code modification detected in: {', '.join(rel_names)}")
                print("[WATCHER] Triggering Autonomous Red-Team Audit & Self-Healing Loop...")
                run_single_audit(heal=True)
                last_mtimes = current_mtimes
    except KeyboardInterrupt:
        print("\n[WATCHER] Stopped.")


def main():
    parser = argparse.ArgumentParser(description="Autonomous Red-Team Code Watcher & Self-Healing Pipeline")
    parser.add_argument("--once", action="store_true", help="Run audit once and exit with returncode")
    parser.add_argument("--no-heal", action="store_true", help="Disable auto-healing patches")
    parser.add_argument("--interval", type=float, default=2.0, help="Watcher poll interval in seconds")
    args = parser.parse_args()

    if args.once:
        sys.exit(run_single_audit(heal=not args.no_heal))
    else:
        run_watcher(poll_interval=args.interval)


if __name__ == "__main__":
    main()
