"""Run every test module. Exit code is non-zero if any of them fail.

    python tests/run_all.py
"""

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
MODULES = [
    "test_patterns.py",
    "test_availability.py",
    "test_export.py",
    "test_web_assets.py",
    "test_recorder_health.py",
]


def main() -> int:
    failed = []
    for module in MODULES:
        print(f"\n{'=' * 70}\n{module}\n{'=' * 70}")
        result = subprocess.run(
            [sys.executable, os.path.join(HERE, module)], check=False
        )
        if result.returncode != 0:
            failed.append(module)

    print(f"\n{'=' * 70}")
    if failed:
        print(f"FAILED: {', '.join(failed)}")
        return 1
    print(f"All {len(MODULES)} test modules passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
