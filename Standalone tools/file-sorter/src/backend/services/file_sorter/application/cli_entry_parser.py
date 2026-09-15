"""CLI argument parser construction for file sorter."""

from __future__ import annotations

import argparse

from ..infrastructure.cleanup import (
    DEFAULT_ANALYSIS_SPEED,
    DEFAULT_SIMILAR_VIDEO_THRESHOLD,
)


def _add_keyword_arguments(parser: argparse.ArgumentParser) -> None:
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



def _add_listing_arguments(parser: argparse.ArgumentParser) -> None:
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



def _add_cleanup_arguments(parser: argparse.ArgumentParser) -> None:
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



def _add_state_arguments(parser: argparse.ArgumentParser) -> None:
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



def _add_plan_arguments(parser: argparse.ArgumentParser) -> None:
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



def _add_profile_arguments(parser: argparse.ArgumentParser) -> None:
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
        "--select-scan-target",
        action="store_true",
        help=(
            "Make the selected folder the single active scan target while "
            "background classification is already active."
        ),
    )
    parser.add_argument(
        "--set-duplicate-trash-enabled",
        choices=("true", "false"),
        help="Opt in or out of exact-duplicate recycling for the target/profile.",
    )



def _add_output_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="輸出 JSON 報告。")
    parser.add_argument(
        "--progress-jsonl",
        action="store_true",
        help="輸出清理掃描進度事件。",
    )



def create_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="使用資料夾名稱與自訂關鍵字，將檔案安全歸檔到對應子資料夾。"
    )
    parser.add_argument("target_dir", nargs="?", default=".", help="要整理的目標目錄")
    _add_keyword_arguments(parser)
    _add_listing_arguments(parser)
    _add_cleanup_arguments(parser)
    _add_state_arguments(parser)
    _add_plan_arguments(parser)
    _add_profile_arguments(parser)
    _add_output_arguments(parser)
    return parser
