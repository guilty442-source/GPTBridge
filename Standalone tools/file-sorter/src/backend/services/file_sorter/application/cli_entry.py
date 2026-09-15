"""CLI argument parser and main entry point for file sorter."""

from __future__ import annotations

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
from ..infrastructure.cleanup import (
    DEFAULT_ANALYSIS_SPEED,
    DEFAULT_SIMILAR_VIDEO_THRESHOLD,
    print_progress_event,
    run_cleanup_scan,
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
    apply_organize_plan,
    configure_duplicate_trash_enabled,
    configure_profile_enabled,
    organize_files,
    preview_organize_files,
    run_enabled_profiles_once,
    scan_after_keyword_addition,
    select_scan_target,
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
    get_rules_path,
    read_custom_rules,
    write_custom_rules,
)
from .cli_entry_parser import create_argument_parser


def print_rules(rules: Iterable[KeywordRule]) -> None:
    rules_list = list(rules)
    if not rules_list:
        print("目前沒有可用的關鍵字規則。")
        return
    print("目前關鍵字規則：")
    for rule in rules_list:
        source_label = "程式碼" if rule.source == "custom" else "資料夾"
        print(f"- [{source_label}] {rule.keyword} → {rule.folder}")



def _print_profile_snapshot(snapshot: ProfileSnapshot) -> int:
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



def _handle_profiles_json(args, state_root) -> int | None:
    if not args.profiles_json:
        return None
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



def _handle_set_profile_enabled(args, target, state_root) -> int | None:
    if args.set_profile_enabled is None:
        return None
    snapshot = configure_profile_enabled(
        target,
        args.set_profile_enabled == "true",
        state_root=state_root,
        profile=args.profile,
    )
    return _print_profile_snapshot(snapshot)



def _handle_select_scan_target(args, target, state_root) -> int | None:
    if not args.select_scan_target:
        return None
    snapshot = select_scan_target(
        target,
        state_root=state_root,
        profile=args.profile,
    )
    return _print_profile_snapshot(snapshot)



def _handle_set_duplicate_trash_enabled(args, target, state_root) -> int | None:
    if args.set_duplicate_trash_enabled is None:
        return None
    snapshot = configure_duplicate_trash_enabled(
        target,
        args.set_duplicate_trash_enabled == "true",
        state_root=state_root,
        profile=args.profile,
    )
    return _print_profile_snapshot(snapshot)



def _handle_history_json(args, target, state_root) -> int | None:
    if not args.history_json:
        return None
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



def _handle_recover(args, target, state_root) -> int | None:
    if not args.recover:
        return None
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



def _handle_prune_state(args, state_root) -> int | None:
    if not args.prune_state:
        return None
    payload = {
        "ok": True,
        "type": "file-sorter-prune-result",
        **prune_state(state_root=state_root),
    }
    print(json.dumps(payload, ensure_ascii=False))
    return 0



def _handle_undo_last(args, target, state_root) -> int | None:
    if not args.undo_last:
        return None
    try:
        payload = undo_last_transaction(
            target,
            state_root=state_root,
        )
    except SorterV2Error as error:
        raise FileSorterError(str(error)) from error
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if payload["ok"] else 1



def _handle_apply_plan(args, target, state_root) -> int | None:
    if not args.apply_plan:
        return None
    payload = apply_organize_plan(
        args.apply_plan,
        target_dir=target,
        state_root=state_root,
        profile=args.profile,
    )
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if payload["ok"] else 1



def _handle_preview(args, target, state_root) -> int | None:
    if not (args.preview_json or args.dry_run):
        return None
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



def _handle_cleanup_scan(args, target) -> int | None:
    if not args.cleanup_scan:
        return None
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



def _print_rules_saved(target, state_root, profile) -> None:
    print(
        f"File Sorter 分類規則已儲存（非主系統治理規則）："
        f"{get_rules_path(target, state_root=state_root, profile=profile)}"
    )



def _print_keyword_scan_result(target, state_root) -> None:
    scan_report = scan_after_keyword_addition(
        target,
        state_root=state_root,
    )
    if scan_report is None:
        print("新關鍵字已儲存；此工作區目前無法執行自動掃描。")
        return
    print(
        "新關鍵字已觸發即時掃描："
        f"移動 {int(scan_report.get('moved_count', 0))} 個檔案，"
        f"等待穩定確認 "
        f"{int(scan_report.get('waiting_for_second_observation_count', 0))} 個。"
    )



def _handle_upsert_keyword(args, target, state_root) -> int | None:
    if not args.upsert_keyword:
        return None
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
    _print_rules_saved(target, state_root, args.profile)
    if upsert_result.added:
        _print_keyword_scan_result(target, state_root)
    return 0



def _handle_add_keyword(args, target, state_root) -> int | None:
    if not args.add_keyword:
        return None
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
    _print_rules_saved(target, state_root, args.profile)
    return 0



def _handle_update_keyword(args, target, state_root) -> int | None:
    if not args.update_keyword:
        return None
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
    _print_rules_saved(target, state_root, args.profile)
    return 0



def _handle_list_folders(args, target) -> int | None:
    if not args.list_folders:
        return None
    folders = list_destination_folders(target)
    print(f"{FOLDERS_JSON_PREFIX}{json.dumps(folders, ensure_ascii=False)}")
    print(f"掃描完成：找到 {len(folders)} 個第一層子資料夾。")
    return 0



def _handle_list_source_files(args, target) -> int | None:
    if not args.list_source_files:
        return None
    source_files = list_source_files(target)
    print(f"{SOURCE_FILES_JSON_PREFIX}{json.dumps(source_files, ensure_ascii=False)}")
    print(f"掃描完成：找到 {len(source_files)} 個待整理檔案。")
    return 0



def _handle_list_keywords(args, target, state_root) -> int | None:
    if not args.list_keywords:
        return None
    print_rules(
        build_keyword_rules(
            target,
            state_root=state_root,
            profile=args.profile,
        )
    )
    return 0



def _run_default_organize(args, target, state_root) -> int:
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



def _dispatch_command(args, target, state_root) -> int | None:
    handlers = (
        lambda: _handle_profiles_json(args, state_root),
        lambda: _handle_set_profile_enabled(args, target, state_root),
        lambda: _handle_select_scan_target(args, target, state_root),
        lambda: _handle_set_duplicate_trash_enabled(args, target, state_root),
        lambda: _handle_history_json(args, target, state_root),
        lambda: _handle_recover(args, target, state_root),
        lambda: _handle_prune_state(args, state_root),
        lambda: _handle_undo_last(args, target, state_root),
        lambda: _handle_apply_plan(args, target, state_root),
        lambda: _handle_preview(args, target, state_root),
        lambda: _handle_cleanup_scan(args, target),
        lambda: _handle_upsert_keyword(args, target, state_root),
        lambda: _handle_add_keyword(args, target, state_root),
        lambda: _handle_update_keyword(args, target, state_root),
        lambda: _handle_list_folders(args, target),
        lambda: _handle_list_source_files(args, target),
        lambda: _handle_list_keywords(args, target, state_root),
    )
    for handler in handlers:
        result = handler()
        if result is not None:
            return result
    return None



def main() -> int:
    args = create_argument_parser().parse_args()
    try:
        target = resolve_target_dir(args.target_dir)
        result = _dispatch_command(args, target, args.state_root)
        if result is not None:
            return result
        return _run_default_organize(args, target, args.state_root)
    except FileSorterError as error:
        print(f"錯誤：{error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
