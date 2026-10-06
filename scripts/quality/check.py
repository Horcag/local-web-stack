"""Portable gate shared by developer hooks and CI; run from repository root."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]


def run(*args: str) -> None:
    print("+ " + " ".join(args), flush=True)
    subprocess.run(args, cwd=ROOT, check=True)


def main() -> None:
    os.chdir(ROOT)
    run("uv", "lock", "--check", "--offline")
    run(sys.executable, "scripts/quality/policy.py")
    run(
        sys.executable,
        "-m",
        "ruff",
        "format",
        "--check",
        "service",
        "mcp",
        "tests",
        "scripts/quality",
    )
    run(sys.executable, "-m", "ruff", "check", "service", "mcp", "tests", "scripts/quality")
    run(sys.executable, "-m", "mypy")
    evidence = Path(os.environ.get("LOCAL_WEB_QUALITY_DIR", str(ROOT / "work" / "quality")))
    evidence.mkdir(parents=True, exist_ok=True)
    run(
        sys.executable,
        "-m",
        "pytest",
        "-v",
        "--cov=service",
        "--cov=mcp",
        "--cov-report=term-missing",
        f"--cov-report=xml:{evidence / 'coverage.xml'}",
        f"--junitxml={evidence / 'tests.xml'}",
    )
    cases = ET.parse(evidence / "tests.xml").findall(".//testcase")
    executed = [case for case in cases if case.find("skipped") is None]
    test_files = {path.stem for path in (ROOT / "tests").rglob("test_*.py")}
    discovered = {part for case in executed for part in case.get("classname", "").split(".")}
    missing = test_files - discovered
    if missing:
        raise SystemExit(f"No executed cases from test modules: {sorted(missing)}")
    if not executed:
        raise SystemExit("No tests executed; discovery receipt is empty")
    print(f"Executed {len(executed)} cases; receipt: {evidence / 'tests.xml'}", flush=True)


if __name__ == "__main__":
    main()
