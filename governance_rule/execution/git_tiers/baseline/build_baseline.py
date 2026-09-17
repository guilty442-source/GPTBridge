"""CLI: build / verify / bind the golden Git baseline (article 522-523).

Usage (PowerShell, from the repository root)::

    main-system\\.venv\\Scripts\\python.exe `
        governance_rule\\execution\\git_tiers\\baseline\\build_baseline.py `
        --gate-evidence <evidence.json> --write --bind

    main-system\\.venv\\Scripts\\python.exe `
        governance_rule\\execution\\git_tiers\\baseline\\build_baseline.py --status
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[4]))

from governance_rule.execution.git_tiers import baseline  # noqa: E402
from governance_rule.execution.git_tiers.baseline import (  # noqa: E402
    build_baseline_payload,
    verify_baseline,
    write_baseline,
)


def _load_evidence(path: str) -> dict:
    if not path:
        return {}
    candidate = Path(path)
    if not candidate.is_file():
        print(f"[WARN] evidence file not found: {path}", file=sys.stderr)
        return {}
    return json.loads(candidate.read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Golden Git baseline tool")
    parser.add_argument("--gate-evidence", default="",
                        help="JSON file with gate results")
    parser.add_argument("--write", action="store_true",
                        help="write baseline + .sha256 sidecar")
    parser.add_argument("--bind", action="store_true",
                        help="register baseline digest in the governance manifest")
    parser.add_argument("--status", action="store_true",
                        help="verify the on-disk baseline")
    parser.add_argument("--out", default="", help="output directory override")
    args = parser.parse_args(argv)

    if args.status:
        result = verify_baseline()
        printable = {k: v for k, v in result.items() if k != "payload"}
        print(json.dumps(printable, ensure_ascii=False, indent=2))
        return 0 if result["mode"] == "ACTIVE" else 1

    evidence = _load_evidence(args.gate_evidence)
    payload = build_baseline_payload(gate_evidence=evidence)
    verdict = payload["verdict"]
    print(f"verdict: {verdict}")
    for blocker in payload["blockers"]:
        print(f"  blocker: {blocker}")
    print("freeze conditions:")
    for row in payload["test_suite"]["freeze_conditions"]:
        print(f"  [{row['status']:<10}] {row['condition']} ({row['zh']})")

    if args.write:
        directory = Path(args.out) if args.out else baseline.BASELINE_DIR
        written = write_baseline(payload, directory)
        print(f"baseline: {written['path']}")
        print(f"digest:   {written['digest']}")
    if args.bind:
        bound = baseline.bind_to_governance_manifest()
        print(f"manifest: {bound['manifest']}")
        print(f"manifest digest: {bound['manifest_digest']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
