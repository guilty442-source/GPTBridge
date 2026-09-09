"""CLI argument parser and main entry point for file sorter."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Iterable

from ..infrastructure.sorter_engine import (
    DEFAULT_QUIET_SECONDS,
    OrganizePlan,
    PlanOperation,
    ProfileSnapshot,
    RuleConflictError,
    SkippedFile,
    SorterV2Error,
    check_file_stability,
    execute_plan,
    list_profiles,
    load_plan,
    load_profile,
    new_plan,
    profile_path,
    prune_state,
    recover_transactions,
    resolve_state_root,
    same_volume,
    save_plan,
    save_profile,
    transaction_history,
    undo_last_transaction,
)
from ..infrastructure.sorter_engine import (
    _ExclusiveFileLock,
    _atomic_write_json,
    _state_category_root,
    _utc_now,
    _validated_state_document_path,
)
from .cli_constants import (
    FOLDERS_JSON_PREFIX,
    SOURCE_FILES_JSON_PREFIX,
    TOOL_ROOT,
)
from .cli_keywords import (
    add_keywords,
    build_keyword_rules,
    update_keyword,
    upsert_keywords,
)
from .cli_models import FileSorterError, KeywordRule, OrganizeResult
from .cli_organize import (
    configure_duplicate_trash_enabled,
    configure_profile_enabled,
    organize_files,
    preview_organize_files,
    run_enabled_profiles_once,
)
from .cli_paths import (
    is_absolute_destination,
    is_local_folder_name,
    list_destination_folders,
    list_source_files,
    normalize_match_text,
    resolve_destination_dir,
    resolve_target_dir,
)
from .cli_rules import (
    _migrate_existing_profile_rules,
    read_custom_rules,
    write_custom_rules,
)


def print_rules(rules: Iterable[KeywordRule]) -> None:
    rules_list = list(rules)
    if not rules_list:
        print("目前沒有可用的關鍵字規則。")
        return
    print("目前關鍵字規則：")
    for rule in rules_list:
        source_label = "程式碼" if rule.source == "custom" else "資料夾"
        print(f"- [{source_label}] {rule.keyword} → {rule.folder}")



def create_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="使用資料夾名稱與自訂關鍵字，將檔案安全歸檔到對應子資料夾。"
    )
    parser.add_argument("target_dir", nargs="?", default=".", help="要整理的目標目錄")
    parser.add_argument(
        "--add-keyword",
        action="append",
        default=[],
        help="追加關鍵字，可重複指定",
    )
    parser.add_argument("--folder", help="追加關鍵字要分類到的子資料夾")
    parser.add_argument("--update-keyword", help="要修改的現有程式碼關鍵字")
    parser.add_argument("--new-keyword", help="修改後的新關鍵字")
    parser.add_argument(
        "--upsert-keyword",
        action="append",
        default=[],
        help="新增或更新關鍵字；已存在時自動改用指定分類資料夾。",
    )
    parser.add_argument(
        "--list-keywords",
        action="store_true",
        help="顯示目前所有有效關鍵字規則",
    )
    parser.add_argument(
        "--list-folders",
        action="store_true",
        help="掃描並顯示目前所有第一層子資料夾",
    )
    parser.add_argument(
        "--list-source-files",
        action="store_true",
        help="列出目前目標資料夾根目錄中可整理的檔案，供自動偵測使用。",
    )
    parser.add_argument(
        "--cleanup-scan",
        action="store_true",
        help="執行合併後的清理掃描，不包含完全重複檔或相似圖片偵測。",
    )
    parser.add_argument(
        "--image-cleanup",
        action="store_true",
        help="列出橫向或非直式圖片候選。",
    )
    parser.add_argument(
        "--similar-image-analysis",
        action="store_true",
        help="使用 MiniCPM-V 分析相似圖片。",
    )
    parser.add_argument(
        "--video-cleanup",
        action="store_true",
        help="列出影片問題候選。",
    )
    parser.add_argument(
        "--similar-video-analysis",
        action="store_true",
        help="保留的相似影片偵測功能。",
    )
    parser.add_argument(
        "--similar-video-threshold",
        type=int,
        default=DEFAULT_SIMILAR_VIDEO_THRESHOLD,
        help="相似影片門檻，1 到 100。",
    )
    parser.add_argument(
        "--analysis-speed",
        type=int,
        default=DEFAULT_ANALYSIS_SPEED,
        help="分析速度，1 到 100；越高採樣越少。",
    )
    parser.add_argument("--no-parallel-analysis", action="store_true")
    parser.add_argument("--model-temperature", type=float, default=0.0)
    parser.add_argument("--model-top-p", type=float, default=0.9)
    parser.add_argument("--model-context-window", type=int, default=8192)
    parser.add_argument("--model-max-output-tokens", type=int, default=512)
    parser.add_argument(
        "--state-root",
        help="V2 state root (or set FILE_SORTER_STATE_ROOT).",
    )
    parser.add_argument("--profile", help="Optional named profile for this target.")
    parser.add_argument(
        "--quiet-seconds",
        "--quiet-period",
        dest="quiet_seconds",
        type=float,
        default=None,
        help="Require an unchanged modification time for this many seconds.",
    )
    parser.add_argument(
        "--preview-json",
        action="store_true",
        help="Persist and print a structured no-change organization plan.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Alias for --preview-json.",
    )
    parser.add_argument(
        "--apply-plan",
        metavar="PLAN_ID",
        help="Apply a previously persisted plan after revalidation.",
    )
    parser.add_argument(
        "--undo-last",
        action="store_true",
        help="Undo the newest committed transaction for the target.",
    )
    parser.add_argument(
        "--history-json",
        action="store_true",
        help="Print transaction history as JSON.",
    )
    parser.add_argument(
        "--recover",
        action="store_true",
        help="Recover interrupted transactions without overwriting files.",
    )
    parser.add_argument(
        "--prune-state",
        action="store_true",
        help="Remove expired plans and configured old terminal journals.",
    )
    parser.add_argument(
        "--profiles-json",
        action="store_true",
        help="Print all V2 profile configurations as JSON.",
    )
    parser.add_argument(
        "--set-profile-enabled",
        choices=("true", "false"),
        help="Enable or disable background runs for the target/profile.",
    )
    parser.add_argument(
        "--set-duplicate-trash-enabled",
        choices=("true", "false"),
        help="Opt in or out of exact-duplicate recycling for the target/profile.",
    )
    parser.add_argument("--json", action="store_true", help="輸出 JSON 報告。")
    parser.add_argument(
        "--progress-jsonl",
        action="store_true",
        help="輸出清理掃描進度事件。",
    )
    return parser



def main() -> int:
    args = create_argument_parser().parse_args()
    try:
        target = resolve_target_dir(args.target_dir)
        state_root = args.state_root

        if args.profiles_json:
            payload = {
                "ok": True,
                "type": "file-sorter-profiles",
                "state_root": str(resolve_state_root(state_root)),
                "profiles": [
                    snapshot.to_dict(include_rules=False)
                    for snapshot in list_profiles(state_root=state_root)
                ],
            }
            print(json.dumps(payload, ensure_ascii=False))
            return 0

        if args.set_profile_enabled is not None:
            snapshot = configure_profile_enabled(
                target,
                args.set_profile_enabled == "true",
                state_root=state_root,
                profile=args.profile,
            )
            print(
                json.dumps(
                    {
                        "ok": True,
                        "type": "file-sorter-profile",
                        "profile": snapshot.to_dict(include_rules=False),
                    },
                    ensure_ascii=False,
                )
            )
            return 0

        if args.set_duplicate_trash_enabled is not None:
            snapshot = configure_duplicate_trash_enabled(
                target,
                args.set_duplicate_trash_enabled == "true",
                state_root=state_root,
                profile=args.profile,
            )
            print(
                json.dumps(
                    {
                        "ok": True,
                        "type": "file-sorter-profile",
                        "profile": snapshot.to_dict(include_rules=False),
                    },
                    ensure_ascii=False,
                )
            )
            return 0

        if args.history_json:
            print(
                json.dumps(
                    {
                        "ok": True,
                        "type": "file-sorter-history",
                        "history": transaction_history(
                            state_root=state_root,
                            target_dir=target,
                        ),
                    },
                    ensure_ascii=False,
                )
            )
            return 0

        if args.recover:
            recovery = recover_transactions(
                state_root=state_root,
                target_dir=target,
            )
            payload = {
                "ok": all(not item.get("errors") for item in recovery),
                "type": "file-sorter-recovery-result",
                "recovered": recovery,
            }
            print(json.dumps(payload, ensure_ascii=False))
            return 0 if payload["ok"] else 1

        if args.prune_state:
            payload = {
                "ok": True,
                "type": "file-sorter-prune-result",
                **prune_state(state_root=state_root),
            }
            print(json.dumps(payload, ensure_ascii=False))
            return 0

        if args.undo_last:
            try:
                payload = undo_last_transaction(
                    target,
                    state_root=state_root,
                )
            except SorterV2Error as error:
                raise FileSorterError(str(error)) from error
            print(json.dumps(payload, ensure_ascii=False))
            return 0 if payload["ok"] else 1

        if args.apply_plan:
            payload = apply_organize_plan(
                args.apply_plan,
                target_dir=target,
                state_root=state_root,
                profile=args.profile,
            )
            print(json.dumps(payload, ensure_ascii=False))
            return 0 if payload["ok"] else 1

        if args.preview_json or args.dry_run:
            plan = preview_organize_files(
                target,
                quiet_seconds=(
                    DEFAULT_QUIET_SECONDS
                    if args.quiet_seconds is None
                    else args.quiet_seconds
                ),
                state_root=state_root,
                profile=args.profile,
                persist=True,
            )
            print(json.dumps(plan.to_dict(), ensure_ascii=False))
            return 0

        if args.cleanup_scan:
            selected_cleanup = bool(
                args.image_cleanup
                or args.similar_image_analysis
                or args.video_cleanup
                or args.similar_video_analysis
            )
            report = run_cleanup_scan(
                target,
                image_cleanup=bool(args.image_cleanup),
                similar_image_analysis=bool(args.similar_image_analysis),
                video_cleanup=bool(
                    args.video_cleanup
                    or args.similar_video_analysis
                    or not selected_cleanup
                ),
                similar_video_analysis=bool(
                    args.similar_video_analysis
                ),
                similar_video_threshold=args.similar_video_threshold,
                analysis_speed=args.analysis_speed,
                parallel_analysis=not bool(args.no_parallel_analysis),
                model_temperature=args.model_temperature,
                model_top_p=args.model_top_p,
                model_context_window=args.model_context_window,
                model_max_output_tokens=args.model_max_output_tokens,
                progress_event_callback=(
                    print_progress_event if args.progress_jsonl else None
                ),
            )
            print(json.dumps(report, ensure_ascii=False, indent=2 if args.json else None))
            return 0 if report.get("ok") is not False else 1

        if args.upsert_keyword:
            if not args.folder:
                raise FileSorterError("新增或更新關鍵字時必須指定分類資料夾。")
            upsert_result = upsert_keywords(
                target,
                args.upsert_keyword,
                args.folder,
                state_root=state_root,
                profile=args.profile,
            )
            for rule in upsert_result.added:
                print(f"已新增關鍵字「{rule.keyword}」→「{rule.folder}」")
            for rule in upsert_result.updated:
                print(f"已更新既有關鍵字「{rule.keyword}」→「{rule.folder}」")
            for rule in upsert_result.unchanged:
                print(f"關鍵字已存在，沿用分類「{rule.keyword}」→「{rule.folder}」")
            print(
                f"File Sorter 分類規則已儲存（非主系統治理規則）："
                f"{get_rules_path(target, state_root=state_root, profile=args.profile)}"
            )
            if upsert_result.added:
                scan_report = scan_after_keyword_addition(
                    target,
                    state_root=state_root,
                )
                if scan_report is None:
                    print("新關鍵字已儲存；此工作區目前無法執行自動掃描。")
                else:
                    print(
                        "新關鍵字已觸發即時掃描："
                        f"移動 {int(scan_report.get('moved_count', 0))} 個檔案，"
                        f"等待穩定確認 "
                        f"{int(scan_report.get('waiting_for_second_observation_count', 0))} 個。"
                    )
            return 0
        if args.add_keyword:
            if not args.folder:
                raise FileSorterError("追加關鍵字時必須指定分類資料夾。")
            added_rules = add_keywords(
                target,
                args.add_keyword,
                args.folder,
                state_root=state_root,
                profile=args.profile,
            )
            for rule in added_rules:
                print(f"已追加關鍵字「{rule.keyword}」→「{rule.folder}」")
            print(
                f"File Sorter 分類規則已儲存（非主系統治理規則）："
                f"{get_rules_path(target, state_root=state_root, profile=args.profile)}"
            )
            return 0

        if args.update_keyword:
            if not args.new_keyword:
                raise FileSorterError("修改關鍵字時必須指定新關鍵字。")
            updated_rule = update_keyword(
                target,
                args.update_keyword,
                args.new_keyword,
                args.folder,
                state_root=state_root,
                profile=args.profile,
            )
            print(f"已修改程式碼關鍵字「{args.update_keyword}」→「{updated_rule.keyword}」")
            print(f"分類資料夾：「{updated_rule.folder}」")
            print(
                f"File Sorter 分類規則已儲存（非主系統治理規則）："
                f"{get_rules_path(target, state_root=state_root, profile=args.profile)}"
            )
            return 0

        if args.list_folders:
            folders = list_destination_folders(target)
            print(f"{FOLDERS_JSON_PREFIX}{json.dumps(folders, ensure_ascii=False)}")
            print(f"掃描完成：找到 {len(folders)} 個第一層子資料夾。")
            return 0

        if args.list_source_files:
            source_files = list_source_files(target)
            print(f"{SOURCE_FILES_JSON_PREFIX}{json.dumps(source_files, ensure_ascii=False)}")
            print(f"掃描完成：找到 {len(source_files)} 個待整理檔案。")
            return 0

        if args.list_keywords:
            print_rules(
                build_keyword_rules(
                    target,
                    state_root=state_root,
                    profile=args.profile,
                )
            )
            return 0

        print(f"開始整理目錄：{target}")
        result = organize_files(
            target,
            quiet_seconds=0.0 if args.quiet_seconds is None else args.quiet_seconds,
            state_root=state_root,
            profile=args.profile,
        )
        for warning in result.warnings:
            print(f"警告：{warning}", file=sys.stderr)
        for error in result.errors:
            print(f"錯誤：{error}", file=sys.stderr)
        print(
            f"歸檔完成：移動 {result.moved_count} 個檔案，"
            f"未匹配 {result.unmatched_count} 個檔案，錯誤 {len(result.errors)} 個。"
        )
        return 1 if result.errors else 0
    except FileSorterError as error:
        print(f"錯誤：{error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
