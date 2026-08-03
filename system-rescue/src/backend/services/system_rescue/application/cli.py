from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from .service import SystemRescueService
from .automatic_repair import CentralAutomaticRepairService
from ..integration.package_rebuilder import ToolPackageRebuilder


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="System Rescue")
    operation = parser.add_mutually_exclusive_group(required=True)
    operation.add_argument("--check", action="store_true")
    operation.add_argument("--repair-owned-storage", action="store_true")
    operation.add_argument("--repair-tool", action="store_true")
    parser.add_argument("--target-tool", default="")
    parser.add_argument("--failure-code", default="TOOL_START_FAILED")
    parser.add_argument("--json", action="store_true", dest="as_json")
    return parser


def _print(result: dict[str, Any], as_json: bool) -> int:
    print(
        json.dumps(result, ensure_ascii=False)
        if as_json
        else result.get("operation", "system-rescue")
    )
    return 0 if result.get("ok") else 1


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    tool_root = Path(__file__).resolve().parents[5]
    project_root = Path(os.environ.get("GPTBRIDGE_PROJECT_ROOT", Path.cwd()))
    service = SystemRescueService(project_root, tool_root)
    package_rebuilder = ToolPackageRebuilder(project_root, tool_root)
    automatic_repair = CentralAutomaticRepairService(
        project_root,
        tool_root,
        package_rebuilder=package_rebuilder.rebuild,
    )
    requester_actor = str(
        os.environ.get("GPTBRIDGE_GOVERNED_REQUESTER_ACTOR") or ""
    ).strip()
    allowed_requesters = {
        "governance/main-system",
        "governance/tool/system-rescue",
    }
    if requester_actor not in allowed_requesters:
        return _print(
            {
                "ok": False,
                "error_code": "PERMISSION_DENIED",
                "message": "PERMISSION_DENIED",
            },
            args.as_json,
        )
    if args.check:
        return _print(service.check(), args.as_json)
    if args.repair_owned_storage:
        return _print(service.repair(), args.as_json)
    return _print(
        automatic_repair.repair_tool(args.target_tool, args.failure_code),
        args.as_json,
    )


if __name__ == "__main__":
    raise SystemExit(main())
