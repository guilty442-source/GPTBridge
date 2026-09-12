"""Rules path resolution and reading for the File Sorter application layer."""

from __future__ import annotations

import ast
import json
import os
import stat as stat_module
from pathlib import Path
from typing import Any, Iterable

from ._constants import (
    JSON_RULES_FILE_PATH,
    PACKAGED_JSON_RULES_FILE_PATH,
    PY_RULES_FILE_PATH,
    RULES_FILE_PATH,
    RULES_VARIABLE_NAME,
)
from .models import FileSorterError, KeywordRule
from .paths import (
    is_absolute_destination,
    normalize_match_text,
    resolve_target_dir,
)


def _uses_explicit_legacy_rules_path() -> bool:
    """Keep the historical test/integration override as a compatibility hook."""

    return RULES_FILE_PATH != JSON_RULES_FILE_PATH


def get_rules_path(
    target_dir: str | Path | None = None,
    *,
    state_root: str | Path | None = None,
    profile: str | None = None,
) -> Path:
    if target_dir is not None and not _uses_explicit_legacy_rules_path():
        # Keyword classification rules are project-owned configuration.
        # Validate the target, but never persist these rules in user profiles.
        resolve_target_dir(target_dir)
        return RULES_FILE_PATH
    if RULES_FILE_PATH.exists():
        return RULES_FILE_PATH
    if RULES_FILE_PATH == JSON_RULES_FILE_PATH:
        if PACKAGED_JSON_RULES_FILE_PATH.exists():
            return PACKAGED_JSON_RULES_FILE_PATH
        if PY_RULES_FILE_PATH.exists():
            return PY_RULES_FILE_PATH
    return RULES_FILE_PATH


def _read_rules_document(rules_path: Path) -> list[Any]:
    try:
        rules_stat = rules_path.lstat()
    except FileNotFoundError:
        return []
    except OSError as error:
        raise FileSorterError(f"無法安全檢查規則檔：{rules_path}；{error}") from error
    attributes = int(getattr(rules_stat, "st_file_attributes", 0) or 0)
    if (
        stat_module.S_ISLNK(rules_stat.st_mode)
        or bool(attributes & 0x400)
        or not stat_module.S_ISREG(rules_stat.st_mode)
    ):
        raise FileSorterError(
            f"規則檔必須是一般檔案，且不得為連結或 reparse point：{rules_path}"
        )

    try:
        raw_text = rules_path.read_text(encoding="utf-8")
    except OSError as error:
        raise FileSorterError(f"無法讀取關鍵字規則檔：{rules_path}；{error}") from error
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
    return raw_rules


def _read_custom_rules_path(rules_path: Path) -> list[KeywordRule]:
    raw_rules = _read_rules_document(rules_path)

    rules: list[KeywordRule] = []
    for index, item in enumerate(raw_rules, start=1):
        if not isinstance(item, dict):
            raise FileSorterError(f"第 {index} 筆關鍵字規則格式錯誤。")
        keyword = str(item.get("keyword", "")).strip()
        folder = str(item.get("folder", "")).strip()
        if not keyword or not normalize_match_text(keyword):
            raise FileSorterError(f"第 {index} 筆關鍵字不可為空白。")
        if (
            not folder
            or is_absolute_destination(folder)
            or folder in {".", ".."}
            or Path(folder).name != folder
            or "/" in folder
            or "\\" in folder
        ):
            raise FileSorterError(
                f"第 {index} 筆分類資料夾必須是目標內的單層子資料夾。"
            )
        rules.append(KeywordRule(keyword=keyword, folder=folder))
    return rules


def read_custom_rules(
    target_dir: str | Path | None = None,
    *,
    state_root: str | Path | None = None,
    profile: str | None = None,
) -> list[KeywordRule]:
    if target_dir is not None:
        resolve_target_dir(target_dir)
    return _read_custom_rules_path(get_rules_path())


def write_custom_rules(
    rules: Iterable[KeywordRule],
    target_dir: str | Path | None = None,
    *,
    state_root: str | Path | None = None,
    profile: str | None = None,
    expected_revision: int | None = None,
) -> None:
    rules_list = [rule for rule in rules if rule.source == "custom"]
    if target_dir is not None:
        resolve_target_dir(target_dir)

    # Runtime edits always target this tool's settings authority. A packaged
    # source file may seed the first edit, but is never modified at runtime.
    rules_path = RULES_FILE_PATH
    rules_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = rules_path.with_suffix(rules_path.suffix + ".tmp")
    json_rules = [
        {"keyword": rule.keyword, "folder": rule.folder}
        for rule in rules_list
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

    return None
