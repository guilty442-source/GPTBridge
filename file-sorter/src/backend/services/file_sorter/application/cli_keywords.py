"""Keyword CRUD and matching for file sorter CLI."""

from __future__ import annotations

import unicodedata
from pathlib import Path
from typing import Any, Iterable

from .cli_constants import RULES_VARIABLE_NAME
from .cli_models import FileSorterError, KeywordRule, KeywordUpsertResult
from .cli_paths import (
    destination_exists_for_rule,
    destination_rule_value,
    is_local_folder_name,
    list_destination_folders,
    normalize_match_text,
    normalize_text,
    resolve_destination_dir,
    resolve_target_dir,
)
from .cli_rules import (
    _read_rules_and_revision,
    read_custom_rules,
    write_custom_rules,
)


def add_keywords(
    target_dir: str | Path,
    keywords: Iterable[str],
    folder: str,
    *,
    state_root: str | Path | None = None,
    profile: str | None = None,
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

    custom_rules, revision = _read_rules_and_revision(
        target,
        state_root=state_root,
        profile=profile,
    )
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
    write_custom_rules(
        rules_by_keyword.values(),
        target,
        state_root=state_root,
        profile=profile,
        expected_revision=revision,
    )
    return added_rules



def upsert_keywords(
    target_dir: str | Path,
    keywords: Iterable[str],
    folder: str,
    *,
    state_root: str | Path | None = None,
    profile: str | None = None,
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

    custom_rules, revision = _read_rules_and_revision(
        target,
        state_root=state_root,
        profile=profile,
    )
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

    write_custom_rules(
        rules_by_keyword.values(),
        target,
        state_root=state_root,
        profile=profile,
        expected_revision=revision,
    )
    return result



def update_keyword(
    target_dir: str | Path,
    current_keyword: str,
    new_keyword: str,
    folder: str | None = None,
    *,
    state_root: str | Path | None = None,
    profile: str | None = None,
) -> KeywordRule:
    target = resolve_target_dir(target_dir)
    current_normalized = normalize_match_text(current_keyword)
    new_cleaned = str(new_keyword).strip()
    new_normalized = normalize_match_text(new_cleaned)
    if not current_normalized:
        raise FileSorterError("目前關鍵字不可為空白。")
    if not new_cleaned or not new_normalized:
        raise FileSorterError("新關鍵字不可為空白。")

    custom_rules, revision = _read_rules_and_revision(
        target,
        state_root=state_root,
        profile=profile,
    )
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
    write_custom_rules(
        updated_rules,
        target,
        state_root=state_root,
        profile=profile,
        expected_revision=revision,
    )
    return updated_rule



def build_keyword_rules(
    target_dir: str | Path,
    *,
    state_root: str | Path | None = None,
    profile: str | None = None,
) -> list[KeywordRule]:
    target = resolve_target_dir(target_dir)
    custom_rules = [
        rule
        for rule in read_custom_rules(
            target,
            state_root=state_root,
            profile=profile,
        )
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
