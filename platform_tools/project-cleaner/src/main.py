from __future__ import annotations

import argparse
import json
from pathlib import Path

from backend.cleanup_service import ProjectCleanupService


def main() -> None:
    parser = argparse.ArgumentParser(description="Project Cleaner")
    parser.add_argument("--cleanup-garbage", action="store_true")
    parser.add_argument("--scope", default="global")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args()

    if args.cleanup_garbage:
        service = ProjectCleanupService(Path.cwd())
        result = service.cleanup_garbage(scope=args.scope, dry_run=args.dry_run)
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
