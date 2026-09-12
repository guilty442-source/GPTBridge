from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from ..infrastructure.cleanup_engine import PROGRESS_JSON_PREFIX
from .service import ProjectCleanupService


def _print_result(result: dict[str, Any], as_json: bool) -> int:
    if as_json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        print(result.get("message", "project cleaner operation completed"))
    return 0 if result.get("ok", False) else 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Global Cleaner")
    operation = parser.add_mutually_exclusive_group()
    operation.add_argument("--cleanup-garbage", action="store_true")
    operation.add_argument("--list-quarantine", action="store_true")
    operation.add_argument("--purge-quarantine", action="store_true")
    operation.add_argument("--restore-quarantine", default="")
    operation.add_argument("--pin-quarantine", default="")
    operation.add_argument("--unpin-quarantine", default="")
    operation.add_argument("--analyze-storage", action="store_true")
    operation.add_argument("--repair-anomalies", action="store_true")
    operation.add_argument("--system-check", action="store_true")
    operation.add_argument("--purge-legacy-artifacts", action="store_true")
    operation.add_argument("--status", action="store_true")
    operation.add_argument("--update-preferences", action="store_true")
    operation.add_argument("--create-managed-backup", action="store_true")
    operation.add_argument("--list-managed-backups", action="store_true")
    operation.add_argument("--extract-managed-backup", action="store_true")
    operation.add_argument("--governed-daily-maintenance", action="store_true")
    operation.add_argument("--clear-managed-temp-files", action="store_true")

    parser.add_argument("--scope", default="global")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--quarantine", action="store_true")
    parser.add_argument("--quarantine-ttl-hours", type=int)
    parser.add_argument("--plan-id", default="")
    parser.add_argument("--plan-token", default="")
    parser.add_argument("--selected-item", action="append", default=[])
    parser.add_argument("--backup-owner", default="")
    parser.add_argument("--backup-path", action="append", default=[])
    parser.add_argument("--temp-root", default="")
    parser.add_argument("--confirm-direct-delete", action="store_true")
    parser.add_argument("--restore-conflict", choices=("skip", "rename"), default="skip")
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
            "GPTBRIDGE_GLOBAL_CLEANER_TARGET_ROOT",
            os.environ.get("GPTBRIDGE_PROJECT_ROOT", Path.cwd()),
        )
    )

    def report_progress(event: dict[str, Any]) -> None:
        if args.progress_jsonl:
            print(f"{PROGRESS_JSON_PREFIX}{json.dumps(event, ensure_ascii=False)}", flush=True)

    service = ProjectCleanupService(project_root, progress_callback=report_progress)

    mutation_requested = any(
        (
            args.purge_quarantine,
            bool(args.restore_quarantine),
            bool(args.pin_quarantine),
            bool(args.unpin_quarantine),
            args.repair_anomalies and not args.dry_run,
            args.purge_legacy_artifacts,
            args.update_preferences,
            args.create_managed_backup,
            args.extract_managed_backup,
            args.governed_daily_maintenance,
            args.clear_managed_temp_files,
            args.cleanup_garbage and not args.dry_run,
        )
    )
    requester_actor = str(
        os.environ.get("GPTBRIDGE_GOVERNED_REQUESTER_ACTOR") or ""
    ).strip()
    if mutation_requested and requester_actor not in {
        "governance/main-system",
        "governance/tool/global-cleaner",
        "governance/tool/system-rescue",
    }:
        return _print_result(
            {
                "ok": False,
                "error_code": "PERMISSION_DENIED",
                "message": "PERMISSION_DENIED",
            },
            args.as_json,
        )

    if args.status:
        return _print_result(service.get_status(), args.as_json)
    if args.list_managed_backups:
        return _print_result(service.list_managed_backups(), args.as_json)
    if args.create_managed_backup:
        return _print_result(
            service.create_managed_backup(args.backup_owner),
            args.as_json,
        )
    if args.extract_managed_backup:
        return _print_result(
            service.extract_managed_backup(
                args.backup_owner,
                args.backup_path,
            ),
            args.as_json,
        )
    if args.governed_daily_maintenance:
        return _print_result(
            service.run_governed_daily_maintenance(),
            args.as_json,
        )
    if args.clear_managed_temp_files:
        return _print_result(
            service.clear_managed_temp_files(args.temp_root),
            args.as_json,
        )
    if args.list_quarantine:
        return _print_result(service.list_quarantine_batches(), args.as_json)
    if args.purge_quarantine:
        return _print_result(
            service.purge_quarantine(
                args.quarantine_ttl_hours,
                permanent=args.force,
            ),
            args.as_json,
        )
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
    if args.system_check:
        return _print_result(
            service.system_health_check(deep=args.deep),
            args.as_json,
        )
    if args.purge_legacy_artifacts:
        return _print_result(
            service.purge_legacy_artifacts(force=args.force),
            args.as_json,
        )
    if args.update_preferences:
        if args.quarantine_ttl_hours is None:
            return _print_result(
                {"ok": False, "message": "a preference value is required"},
                args.as_json,
            )
        return _print_result(
            service.update_preferences(
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

    workspace = Path(__file__).resolve().parents[5]
    return _print_result(
        {
            "ok": True,
            "message": "Global Cleaner is registered as a standalone GPTBridge application.",
            "project_folder": str(workspace),
        },
        args.as_json,
    )


if __name__ == "__main__":
    raise SystemExit(main())
