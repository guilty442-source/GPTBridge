from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src-core"))

from core.environment_doctor import (  # noqa: E402
    collect_environment_report,
    format_environment_report,
    repair_electron_runtime,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Diagnose the local GPTBridge development environment.")
    parser.add_argument("--json", action="store_true", dest="as_json", help="Print machine-readable JSON.")
    parser.add_argument("--strict", action="store_true", help="Exit with code 1 when any required check fails.")
    parser.add_argument("--fix-electron", action="store_true", help="Repair a missing local Electron runtime when possible.")
    args = parser.parse_args()

    repair_result = None
    if args.fix_electron:
        repair_result = repair_electron_runtime(PROJECT_ROOT)

    report = collect_environment_report(PROJECT_ROOT)
    if repair_result is not None:
        report["electron_repair"] = repair_result

    if args.as_json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(format_environment_report(report))
        if repair_result is not None:
            print("")
            print(f"Electron repair: {'OK' if repair_result.get('ok') else 'FAIL'} ({repair_result.get('method')})")

    if args.strict and not report["ok"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
