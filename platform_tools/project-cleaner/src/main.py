from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from backend.cleanup_service import ProjectCleanupService


def main() -> None:
    parser = argparse.ArgumentParser(description="Project Cleaner")
    parser.add_argument("--cleanup-garbage", action="store_true")
    parser.add_argument("--scope", default="global")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--quarantine", action="store_true")
    parser.add_argument("--quarantine-ttl-hours", type=int, default=24)
    parser.add_argument("--list-quarantine", action="store_true")
    parser.add_argument("--purge-quarantine", action="store_true")
    parser.add_argument("--restore-quarantine", default="")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args()

    project_root = Path(os.environ.get("GPTBRIDGE_PROJECT_ROOT", Path.cwd()))
    service = ProjectCleanupService(project_root)

    if args.status:
        result = service.get_status()
        if args.as_json:
            print(json.dumps(result, ensure_ascii=False))
        else:
            print(result.get("message", "project cleaner status ready"))
        return

    if args.list_quarantine:
        result = service.list_quarantine_batches()
        if args.as_json:
            print(json.dumps(result, ensure_ascii=False))
        else:
            print(result.get("message", "quarantine batches listed"))
        return

    if args.purge_quarantine:
        result = service.purge_quarantine(args.quarantine_ttl_hours)
        if args.as_json:
            print(json.dumps(result, ensure_ascii=False))
        else:
            print(result.get("message", "quarantine purge completed"))
        return

    if args.restore_quarantine:
        result = service.restore_quarantine(args.restore_quarantine)
        if args.as_json:
            print(json.dumps(result, ensure_ascii=False))
        else:
            print(result.get("message", "quarantine restore completed"))
        return

    if args.cleanup_garbage:
        result = service.cleanup_garbage(
            scope=args.scope,
            dry_run=args.dry_run,
            quarantine=args.quarantine,
            quarantine_ttl_hours=args.quarantine_ttl_hours,
        )
        if args.as_json:
            print(json.dumps(result, ensure_ascii=False))
        else:
            print(result.get("message", "cleanup completed"))
        return

    workspace = Path(__file__).resolve().parent.parent
    message = {
        "ok": True,
        "message": "Project Cleaner is registered as a standalone GPTBridge application.",
        "project_folder": str(workspace),
    }
    if args.as_json:
        print(json.dumps(message, ensure_ascii=False))
    else:
        print(message["message"])
        print(f"Project folder: {workspace}")
        print("Open Project Cleaner from the Applications screen.")


if __name__ == "__main__":
    main()
