#!/usr/bin/env python3
"""Run every hermetic Globus check in isolated Python processes.

The older behavioural checks under ``tests/`` intentionally stub modules in
``sys.modules`` at import time. Running them all in one discovery process lets
those stubs leak between files, so this runner gives each test file a clean
interpreter while keeping one reliable command for contributors and judges.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def run(label: str, command: list[str]) -> bool:
    print(f"\n=== {label} ===", flush=True)
    completed = subprocess.run(command, cwd=ROOT, check=False)
    if completed.returncode:
        print(f"FAILED ({completed.returncode}): {' '.join(command)}", flush=True)
        return False
    return True


def main() -> int:
    python = sys.executable
    checks: list[tuple[str, list[str]]] = [
        (
            "Compile Python sources",
            [
                python,
                "-m",
                "compileall",
                "-q",
                "globus_truth",
                "server",
                "scripts",
                "tests",
            ],
        ),
        (
            "Truth Layer unit and HTTP tests",
            [
                python,
                "-m",
                "unittest",
                "discover",
                "-s",
                "globus_truth/tests",
                "-v",
            ],
        ),
    ]

    checks.extend(
        (
            f"Behavioural check: {path.name}",
            [python, str(path.relative_to(ROOT))],
        )
        for path in sorted((ROOT / "tests").glob("test_*.py"))
    )

    failures = [label for label, command in checks if not run(label, command)]
    if failures:
        print("\nOne or more checks failed:", flush=True)
        for label in failures:
            print(f"  - {label}", flush=True)
        return 1

    print(f"\nAll {len(checks)} check groups passed.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
