"""自動化檔案管理工具。"""

from __future__ import annotations

import argparse
import ast
import json
import re
import shutil
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


RULES_VARIABLE_NAME = "KEYWORD_RULES"
JSON_RULES_FILE_PATH = Path(__file__).with_name("keyword_rules.json")
PY_RULES_FILE_PATH = Path(__file__).with_name("keyword_rules.py")
RULES_FILE_PATH = JSON_RULES_FILE_PATH
LEGACY_RULES_FILE_NAME = ".file-sorter-rules.json"
FOLDERS_JSON_PREFIX = "FILE_SORTER_FOLDERS_JSON="
SOURCE_FILES_JSON_PREFIX = "FILE_SORTER_SOURCE_FILES_JSON="


class FileSorterError(Exception):
    """檔案分類規則或路徑錯誤。"""


@dataclass(frozen=True)
class KeywordRule:
    keyword: str
    folder: str
    source: str = "custom"


@dataclass
class OrganizeResult:
    moved_count: int = 0
    unmatched_count: int = 0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    rules: list[KeywordRule] = field(default_factory=list)


@dataclass
class KeywordUpsertResult:
    added: list[KeywordRule] = field(default_factory=list)
    updated: list[KeywordRule] = field(default_factory=list)
    unchanged: list[KeywordRule] = field(default_factory=list)


def normalize_text(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold().strip()


def normalize_match_text(value: str) -> str:
    return re.sub(r"[\s_]+", " ", normalize_text(value))


def resolve_target_dir(target_dir: str | Path) -> Path:
    target = Path(target_dir).expanduser().resolve()
    if not target.is_dir():
        raise FileSorterError(f"找不到目錄：{target}")
    return target


def is_absolute_destination(folder: str) -> bool:
    return Path(folder.strip()).expanduser().is_absolute()


def is_local_folder_name(folder: str) -> bool:
    folder_name = folder.strip()
    return (
        bool(folder_name)
        and folder_name not in {".", ".."}
        and Path(folder_name).name == folder_name
        and "/" not in folder_name
        and "\\" not in folder_name
    )


def resolve_destination_dir(
    target_dir: Path,
    folder: str,
) -> Path:
    folder_name = folder.strip()
    if is_absolute_destination(folder_name):
        destination = Path(folder_name).expanduser().resolve()
        if destination.exists() and not destination.is_dir():
            raise FileSorterError(f"分類目的地不是資料夾：{destination}")
        if not destination.is_dir():
            raise FileSorterError(f"指定分類資料夾不存在：{destination}")
        return destination
    if (
        not folder_name
        or folder_name in {".", ".."}
        or Path(folder_name).name != folder_name
        or "/" in folder_name
        or "\\" in folder_name
    ):
        raise FileSorterError("分類資料夾必須是目標目錄內的單層資料夾名稱。")

    destination = (target_dir / folder_name).resolve()
    if destination.parent != target_dir:
        raise FileSorterError("分類資料夾不可離開目標目錄。")
    if destination.exists() and not destination.is_dir():
        raise FileSorterError(f"分類目的地不是資料夾：{folder_name}")
    if not destination.is_dir():
        raise FileSorterError(f"分類資料夾不存在：{folder_name}；禁止自動建立子資料夾。")
    return destination


def destination_rule_value(folder: str, destination: Path) -> str:
    if is_absolute_destination(folder):
        return str(destination)
    return destination.name


def destination_exists_for_rule(target_dir: Path, folder: str) -> bool:
    try:
        resolve_destination_dir(target_dir, folder)
    except FileSorterError:
        return False
    return True


def list_destination_folders(target_dir: str | Path) -> list[str]:
    target = resolve_target_dir(target_dir)
    return [
        item.name
        for item in sorted(target.iterdir(), key=lambda path: normalize_text(path.name))
        if item.is_dir() and not item.name.startswith(".")
    ]


def list_source_files(target_dir: str | Path) -> list[dict[str, int | str]]:
    target = resolve_target_dir(target_dir)
    files: list[dict[str, int | str]] = []
    for item in sorted(target.iterdir(), key=lambda path: normalize_text(path.name)):
        if not item.is_file() or item.name == LEGACY_RULES_FILE_NAME:
            continue
        try:
            stat = item.stat()
        except OSError:
            continue
        files.append(
            {
                "name": item.name,
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
            }
        )
    return files


def get_rules_path() -> Path:
    if RULES_FILE_PATH.exists():
        return RULES_FILE_PATH
    if RULES_FILE_PATH == JSON_RULES_FILE_PATH and PY_RULES_FILE_PATH.exists():
        return PY_RULES_FILE_PATH
    return RULES_FILE_PATH


def read_custom_rules() -> list[KeywordRule]:
    rules_path = get_rules_path()
    if not rules_path.exists():
        return []

    raw_text = rules_path.read_text(encoding="utf-8")
    try:
        raw_rules = json.loads(raw_text)
    except json.JSONDecodeError:
        if rules_path.suffix.lower() != ".py":
            raise FileSorterError(f"關鍵字規則檔損壞：{rules_path}")
        try:
            module = ast.parse(raw_text, filename=str(rules_path))
        except SyntaxError as error:
            raise FileSorterError(f"關鍵字規則檔損壞：{rules_path}；{error}") from error

        raw_rules = []
        try:
            for statement in module.body:
                if (
                    isinstance(statement, ast.Assign)
                    and any(
                        isinstance(target, ast.Name) and target.id == RULES_VARIABLE_NAME
                        for target in statement.targets
                    )
                ):
                    raw_rules = ast.literal_eval(statement.value)
                    break
                if (
                    isinstance(statement, ast.AnnAssign)
                    and isinstance(statement.target, ast.Name)
                    and statement.target.id == RULES_VARIABLE_NAME
                    and statement.value is not None
                ):
                    raw_rules = ast.literal_eval(statement.value)
                    break
        except (ValueError, TypeError) as error:
            raise FileSorterError(f"關鍵字規則檔只能包含靜態規則資料：{rules_path}") from error

    if not isinstance(raw_rules, list):
        raise FileSorterError(f"關鍵字規則檔格式錯誤：{rules_path}")

    rules: list[KeywordRule] = []
    for index, item in enumerate(raw_rules, start=1):
        if not isinstance(item, dict):
            raise FileSorterError(f"第 {index} 筆關鍵字規則格式錯誤。")
        keyword = str(item.get("keyword", "")).strip()
        folder = str(item.get("folder", "")).strip()
        if not keyword or not normalize_match_text(keyword):
            raise FileSorterError(f"第 {index} 筆關鍵字不可為空白。")
        if is_absolute_destination(folder):
            pass
        elif (
            not folder
            or folder in {".", ".."}
            or Path(folder).name != folder
            or "/" in folder
            or "\\" in folder
        ):
            raise FileSorterError(f"第 {index} 筆分類資料夾格式錯誤。")
        rules.append(KeywordRule(keyword=keyword, folder=folder))
    return rules


def write_custom_rules(rules: Iterable[KeywordRule]) -> None:
    rules_path = get_rules_path()
    temporary_path = rules_path.with_suffix(rules_path.suffix + ".tmp")
    json_rules = [
        {"keyword": rule.keyword, "folder": rule.folder}
        for rule in rules
        if rule.source == "custom"
    ]
    try:
        temporary_path.write_text(
            json.dumps(json_rules, ensure_ascii=False, indent=4) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        temporary_path.replace(rules_path)
    except OSError as error:
        temporary_path.unlink(missing_ok=True)
        raise FileSorterError(f"無法保存關鍵字規則：{error}") from error


def add_keywords(
    target_dir: str | Path,
    keywords: Iterable[str],
    folder: str,
) -> list[KeywordRule]:
    target = resolve_target_dir(target_dir)
    destination = resolve_destination_dir(target, folder)
    cleaned_keywords: dict[str, str] = {}
    for keyword in keywords:
        cleaned = str(keyword).strip()
        normalized = normalize_match_text(cleaned)
        if cleaned and normalized:
            cleaned_keywords[normalized] = cleaned
    if not cleaned_keywords:
        raise FileSorterError("至少需要一個非空白關鍵字。")

    custom_rules = read_custom_rules()
    rules_by_keyword = {
        normalize_match_text(rule.keyword): rule
        for rule in custom_rules
    }
    added_rules: list[KeywordRule] = []
    for normalized, keyword in cleaned_keywords.items():
        if normalized in rules_by_keyword:
            raise FileSorterError(f"關鍵字已存在：{rules_by_keyword[normalized].keyword}；請使用修改功能。")
        rule = KeywordRule(
            keyword=keyword,
            folder=destination_rule_value(folder, destination),
        )
        rules_by_keyword[normalized] = rule
        added_rules.append(rule)
    write_custom_rules(rules_by_keyword.values())
    return added_rules


def upsert_keywords(
    target_dir: str | Path,
    keywords: Iterable[str],
    folder: str,
) -> KeywordUpsertResult:
    target = resolve_target_dir(target_dir)
    destination = resolve_destination_dir(target, folder)
    destination_value = destination_rule_value(folder, destination)
    cleaned_keywords: dict[str, str] = {}
    for keyword in keywords:
        cleaned = str(keyword).strip()
        normalized = normalize_match_text(cleaned)
        if cleaned and normalized:
            cleaned_keywords[normalized] = cleaned
    if not cleaned_keywords:
        raise FileSorterError("請至少輸入一個有效關鍵字。")

    custom_rules = read_custom_rules()
    rules_by_keyword = {
        normalize_match_text(rule.keyword): rule
        for rule in custom_rules
    }
    result = KeywordUpsertResult()
    for normalized, keyword in cleaned_keywords.items():
        existing_rule = rules_by_keyword.get(normalized)
        next_rule = KeywordRule(keyword=keyword, folder=destination_value)
        if existing_rule is None:
            result.added.append(next_rule)
        elif existing_rule.keyword != next_rule.keyword or existing_rule.folder != next_rule.folder:
            result.updated.append(next_rule)
        else:
            result.unchanged.append(existing_rule)
            next_rule = existing_rule
        rules_by_keyword[normalized] = next_rule

    write_custom_rules(rules_by_keyword.values())
    return result


def update_keyword(
    target_dir: str | Path,
    current_keyword: str,
    new_keyword: str,
    folder: str | None = None,
) -> KeywordRule:
    target = resolve_target_dir(target_dir)
    current_normalized = normalize_match_text(current_keyword)
    new_cleaned = str(new_keyword).strip()
    new_normalized = normalize_match_text(new_cleaned)
    if not current_normalized:
        raise FileSorterError("目前關鍵字不可為空白。")
    if not new_cleaned or not new_normalized:
        raise FileSorterError("新關鍵字不可為空白。")

    custom_rules = read_custom_rules()
    current_rule = next(
        (
            rule
            for rule in custom_rules
            if normalize_match_text(rule.keyword) == current_normalized
        ),
        None,
    )
    if current_rule is None:
        raise FileSorterError(f"找不到可修改的程式碼關鍵字：{current_keyword}")

    destination = resolve_destination_dir(
        target,
        folder.strip() if folder and folder.strip() else current_rule.folder,
    )
    conflicting_rule = next(
        (
            rule
            for rule in custom_rules
            if normalize_match_text(rule.keyword) == new_normalized
            and normalize_match_text(rule.keyword) != current_normalized
        ),
        None,
    )
    if conflicting_rule is not None:
        raise FileSorterError(f"新關鍵字已存在：{conflicting_rule.keyword}")

    updated_rule = KeywordRule(
        keyword=new_cleaned,
        folder=destination_rule_value(
            folder.strip() if folder and folder.strip() else current_rule.folder,
            destination,
        ),
    )
    updated_rules = [
        updated_rule
        if normalize_match_text(rule.keyword) == current_normalized
        else rule
        for rule in custom_rules
    ]
    write_custom_rules(updated_rules)
    return updated_rule


def build_keyword_rules(target_dir: str | Path) -> list[KeywordRule]:
    target = resolve_target_dir(target_dir)
    custom_rules = [
        rule
        for rule in read_custom_rules()
        if destination_exists_for_rule(target, rule.folder)
    ]
    automatic_rules = [
        KeywordRule(keyword=folder, folder=folder, source="folder")
        for folder in list_destination_folders(target)
    ]

    rules_by_keyword: dict[str, KeywordRule] = {}
    for rule in [*custom_rules, *automatic_rules]:
        normalized = normalize_match_text(rule.keyword)
        if normalized and normalized not in rules_by_keyword:
            rules_by_keyword[normalized] = rule

    return sorted(
        rules_by_keyword.values(),
        key=lambda rule: (
            -len(normalize_match_text(rule.keyword)),
            0 if rule.source == "custom" else 1,
            normalize_match_text(rule.keyword),
            normalize_text(rule.folder),
        ),
    )


def keyword_matches(file_stem: str, keyword: str) -> bool:
    normalized_stem = normalize_match_text(file_stem)
    normalized_keyword = normalize_match_text(keyword)
    if not normalized_keyword:
        return False

    if not all(character.isascii() and character.isalnum() for character in normalized_keyword):
        return normalized_keyword in normalized_stem

    search_from = 0
    while True:
        position = normalized_stem.find(normalized_keyword, search_from)
        if position < 0:
            return False
        before = normalized_stem[position - 1] if position > 0 else ""
        end = position + len(normalized_keyword)
        after = normalized_stem[end] if end < len(normalized_stem) else ""
        before_is_ascii_word = bool(before and before.isascii() and before.isalnum())
        after_is_ascii_word = bool(after and after.isascii() and after.isalnum())
        if not before_is_ascii_word and not after_is_ascii_word:
            return True
        search_from = position + 1


def unique_destination(destination_dir: Path, file_name: str) -> Path:
    destination = destination_dir / file_name
    if not destination.exists():
        return destination

    source_name = Path(file_name)
    for index in range(1, 100_000):
        candidate = destination_dir / f"{source_name.stem}_{index}{source_name.suffix}"
        if not candidate.exists():
            return candidate
    raise FileSorterError(f"無法為同名檔案產生安全名稱：{file_name}")


def organize_files(target_dir: str | Path) -> OrganizeResult:
    target = resolve_target_dir(target_dir)
    result = OrganizeResult(rules=build_keyword_rules(target))
    if not result.rules:
        result.errors.append("沒有可用的關鍵字規則。請建立分類資料夾或追加關鍵字。")
        return result

    files = [
        item
        for item in sorted(target.iterdir(), key=lambda path: normalize_text(path.name))
        if item.is_file() and item.name != LEGACY_RULES_FILE_NAME
    ]
    for item in files:
        matched_rule = next(
            (rule for rule in result.rules if keyword_matches(item.stem, rule.keyword)),
            None,
        )
        if matched_rule is None:
            result.unmatched_count += 1
            continue

        try:
            destination_dir = resolve_destination_dir(target, matched_rule.folder)
            destination = unique_destination(destination_dir, item.name)
            shutil.move(str(item), str(destination))
            result.moved_count += 1
            print(
                f"已將檔案「{item.name}」移動至「{matched_rule.folder}」"
                f"（關鍵字：{matched_rule.keyword}）"
            )
        except (OSError, FileSorterError) as error:
            result.errors.append(f"移動「{item.name}」失敗：{error}")

    return result


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
    return parser


def main() -> int:
    args = create_argument_parser().parse_args()
    try:
        target = resolve_target_dir(args.target_dir)
        if args.upsert_keyword:
            if not args.folder:
                raise FileSorterError("新增或更新關鍵字時必須指定分類資料夾。")
            upsert_result = upsert_keywords(target, args.upsert_keyword, args.folder)
            for rule in upsert_result.added:
                print(f"已新增關鍵字「{rule.keyword}」→「{rule.folder}」")
            for rule in upsert_result.updated:
                print(f"已更新既有關鍵字「{rule.keyword}」→「{rule.folder}」")
            for rule in upsert_result.unchanged:
                print(f"關鍵字已存在，沿用分類「{rule.keyword}」→「{rule.folder}」")
            print(f"規則已寫入程式碼：{get_rules_path()}")
            return 0
        if args.add_keyword:
            if not args.folder:
                raise FileSorterError("追加關鍵字時必須指定分類資料夾。")
            added_rules = add_keywords(target, args.add_keyword, args.folder)
            for rule in added_rules:
                print(f"已追加關鍵字「{rule.keyword}」→「{rule.folder}」")
            print(f"規則已寫入程式碼：{get_rules_path()}")
            return 0

        if args.update_keyword:
            if not args.new_keyword:
                raise FileSorterError("修改關鍵字時必須指定新關鍵字。")
            updated_rule = update_keyword(
                target,
                args.update_keyword,
                args.new_keyword,
                args.folder,
            )
            print(f"已修改程式碼關鍵字「{args.update_keyword}」→「{updated_rule.keyword}」")
            print(f"分類資料夾：「{updated_rule.folder}」")
            print(f"規則已寫入程式碼：{get_rules_path()}")
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
            print_rules(build_keyword_rules(target))
            return 0

        print(f"開始整理目錄：{target}")
        result = organize_files(target)
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
