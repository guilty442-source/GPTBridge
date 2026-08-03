from __future__ import annotations

import argparse
import json
from dataclasses import asdict

from governance_policy import governance_policy_snapshot


def main() -> int:
    parser = argparse.ArgumentParser(description="Governance Rule")
    parser.add_argument("--snapshot", action="store_true", required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    snapshot = asdict(governance_policy_snapshot())
    print(json.dumps(snapshot, ensure_ascii=False) if args.json else snapshot)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
