from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from backend.cleanup_engine import PROGRESS_JSON_PREFIX
from backend.cleanup_service import ProjectCleanupService


def _print_result(result: dict[str, Any], as_json: bool) -> int:
    if as_json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        print(result.get("message", "project cleaner operation completed"))
    return 0 if result.get("ok", False) else 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Project Cleaner")
    operation = parser.add_mutually_exclusive_group()
    operation.add_argument("--cleanup-garbage", action="store_true")
    operation.add_argument("--list-quarantine", action="store_true")
    operation.add_argument("--purge-quarantine", action="store_true")
    operation.add_argument("--restore-quarantine", default="")
    operation.add_argument("--pin-quarantine", default="")
    operation.add_argument("--unpin-quarantine", default="")
    operation.add_argument("--analyze-storage", action="store_true")
    operation.add_argument("--repair-anomalies", action="store_true")
    operation.add_argument("--system-rescue-check", action="store_true")
    operation.add_argument("--system-rescue-repair", action="store_true")
    operation.add_argument("--purge-legacy-artifacts", action="store_true")
    operation.add_argument("--auto-clean", action="store_true")
    operation.add_argument("--status", action="store_true")
    operation.add_argument("--update-preferences", action="store_true")

    parser.add_argument("--scope", default="global")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--quarantine", action="store_true")
    parser.add_argument("--quarantine-ttl-hours", type=int)
    parser.add_argument("--plan-id", default="")
    parser.add_argument("--plan-token", default="")
    parser.add_argument("--selected-item", action="append", default=[])
    parser.add_argument("--confirm-direct-delete", action="store_true")
    parser.add_argument("--restore-conflict", choices=("skip", "rename"), default="skip")
    parser.add_argument("--set-automation", choices=("enabled", "disabled"))
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--deep", action="store_true")
    parser.add_argument("--progress-jsonl", action="store_true")
    parser.add_argument("--json", action="store_true", dest="as_json")
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8", errors="replace")
    args = _parser().parse_args(argv)
    project_root = Path(
        os.environ.get(
            "GPTBRIDGE_CLEANER_TARGET_ROOT",
            os.environ.get("GPTBRIDGE_PROJECT_ROOT", Path.cwd()),
        )
    )

    def report_progress(event: dict[str, Any]) -> None:
        if args.progress_jsonl:
            print(f"{PROGRESS_JSON_PREFIX}{json.dumps(event, ensure_ascii=False)}", flush=True)

    service = ProjectCleanupService(project_root, progress_callback=report_progress)

    if args.status:
        return _print_result(service.get_status(), args.as_json)
    if args.list_quarantine:
        return _print_result(service.list_quarantine_batches(), args.as_json)
    if args.purge_quarantine:
        return _print_result(service.purge_quarantine(args.quarantine_ttl_hours), args.as_json)
    if args.restore_quarantine:
        return _print_result(
            service.restore_quarantine(
                args.restore_quarantine,
                conflict_strategy=args.restore_conflict,
            ),
            args.as_json,
        )
    if args.pin_quarantine:
        return _print_result(service.set_quarantine_pinned(args.pin_quarantine, True), args.as_json)
    if args.unpin_quarantine:
        return _print_result(service.set_quarantine_pinned(args.unpin_quarantine, False), args.as_json)
    if args.analyze_storage:
        return _print_result(service.analyze_storage(args.scope), args.as_json)
    if args.repair_anomalies:
        return _print_result(
            service.repair_anomalies(
                dry_run=args.dry_run,
                include_shared=args.scope in {
                    "",
                    "project",
                    "global",
                    "all",
                    "full_project",
                },
            ),
            args.as_json,
        )
    if args.system_rescue_check:
        return _print_result(
            service.system_rescue_check(deep=args.deep),
            args.as_json,
        )
    if args.system_rescue_repair:
        return _print_result(
            service.system_rescue_repair(deep=args.deep),
            args.as_json,
        )
    if args.purge_legacy_artifacts:
        return _print_result(
            service.purge_legacy_artifacts(force=args.force),
            args.as_json,
        )
    if args.auto_clean:
        return _print_result(
            service.automatic_cleanup(
                force=args.force,
                requested_scope=args.scope,
            ),
            args.as_json,
        )
    if args.update_preferences:
        enabled = None if args.set_automation is None else args.set_automation == "enabled"
        if enabled is None and args.quarantine_ttl_hours is None:
            return _print_result(
                {"ok": False, "message": "a preference value is required"},
                args.as_json,
            )
        return _print_result(
            service.update_preferences(
                automation_enabled=enabled,
                quarantine_ttl_hours=args.quarantine_ttl_hours,
            ),
            args.as_json,
        )
    if args.cleanup_garbage:
        return _print_result(
            service.cleanup_garbage(
                scope=args.scope,
                dry_run=args.dry_run,
                quarantine=args.quarantine,
                quarantine_ttl_hours=args.quarantine_ttl_hours,
                plan_id=args.plan_id,
                plan_token=args.plan_token,
                selected_item_ids=args.selected_item,
                confirm_direct_delete=args.confirm_direct_delete,
            ),
            args.as_json,
        )

    workspace = Path(__file__).resolve().parent.parent
    return _print_result(
        {
            "ok": True,
            "message": "Project Cleaner is registered as a standalone GPTBridge application.",
            "project_folder": str(workspace),
        },
        args.as_json,
    )


if __name__ == "__main__":
    raise SystemExit(main())
