#!/usr/bin/env python
"""P1-9 evidence: governed test-target <=20s SLA measurement.

Every ``test_targets`` entry declared in each toolbox-visible independent
tool's ``manifest.json`` is a governed verification unit: each target is run
individually under the shared ``main-system`` venv pytest, its wall time is
measured, and every target must complete within ``--sla`` seconds (the 20s
test contract) with a passing result.

Evidence JSON defaults to
``governance_rule/execution/audit/convergence/p6-tool-test-sla-<date>.json``.
Exits 0 when every declared target passes within the SLA.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS_DIR = ROOT / "Standalone tools"
VENV_PY = ROOT / "main-system" / ".venv" / "Scripts" / "python.exe"
CONVERGENCE_DIR = ROOT / "governance_rule" / "execution" / "audit" / "convergence"

TOOLS = [
    "ai-assistant",
    "ai-collaboration",
    "file-sorter",
    "investment-mobile",
    "local-model",
    "model-dialogue",
    "vaultly",
]


def _tool_dir(tool_id: str) -> Path:
    """Resolve a tool id to its directory via manifest scan (toolbox semantics)."""
    for manifest_path in TOOLS_DIR.rglob("manifest.json"):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if str(manifest.get("id") or "").strip() == tool_id:
            return manifest_path.parent
    raise FileNotFoundError(f"tool manifest not found: {tool_id}")


def _targets(tool_dir: Path) -> list[str]:
    manifest = json.loads(
        (tool_dir / "manifest.json").read_text(encoding="utf-8")
    )
    return [str(t) for t in manifest.get("test_targets") or []]


def _measure(args: argparse.Namespace) -> dict:
    tools: list[dict] = []
    for tool_id in TOOLS:
        tool_dir = _tool_dir(tool_id)
        targets = _targets(tool_dir)
        target_rows: list[dict] = []
        tool_started = time.monotonic()
        for target in targets:
            command = [
                str(VENV_PY),
                "-m",
                "pytest",
                "-q",
                "--no-header",
                target,
            ]
            started = time.monotonic()
            try:
                completed = subprocess.run(
                    command,
                    cwd=str(tool_dir),
                    capture_output=True,
                    text=True,
                    timeout=args.timeout,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                elapsed = time.monotonic() - started
                tail = (completed.stdout or "").strip().splitlines()[-2:]
                target_rows.append(
                    {
                        "target": target,
                        "seconds": round(elapsed, 2),
                        "returncode": completed.returncode,
                        "tail": tail,
                        "ok": completed.returncode == 0
                        and elapsed <= args.sla,
                        "within_sla": elapsed <= args.sla,
                    }
                )
            except subprocess.TimeoutExpired:
                target_rows.append(
                    {
                        "target": target,
                        "seconds": None,
                        "ok": False,
                        "within_sla": False,
                        "error": f"timeout>{args.timeout}s",
                    }
                )
        tools.append(
            {
                "tool_id": tool_id,
                "tool_dir": str(tool_dir.relative_to(ROOT)),
                "suite_seconds": round(time.monotonic() - tool_started, 2),
                "targets": target_rows,
                "ok": bool(target_rows) and all(r["ok"] for r in target_rows),
            }
        )
    return {
        "evidence": "governed test targets <=20s each (P1-9)",
        "schema": "star-tool-test-sla/v1",
        "at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "sla_seconds": args.sla,
        "tools": tools,
        "ok": all(t["ok"] for t in tools),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sla", type=float, default=20.0)
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--report", default=None)
    args = parser.parse_args()

    report = _measure(args)
    if args.report:
        out = Path(args.report)
    else:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
        out = CONVERGENCE_DIR / f"p6-tool-test-sla-{stamp}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    worst = max(
        (
            r["seconds"] or 0.0
            for t in report["tools"]
            for r in t["targets"]
        ),
        default=0.0,
    )
    print(f"tool-test-sla ok={report['ok']} worst_target={worst}s report={out}")
    for tool in report["tools"]:
        print(
            f"  {tool['tool_id']:<20} suite={tool['suite_seconds']}s "
            f"targets={len(tool['targets'])} ok={tool['ok']}"
        )
        for row in tool["targets"]:
            flag = "" if row["ok"] else "  <-- FAIL"
            print(
                f"    {row['target']:<58} {row.get('seconds')}s{flag}"
            )
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
