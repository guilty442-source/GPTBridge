"""Legacy rules migration for the File Sorter application layer."""

from __future__ import annotations

import hashlib
import json
import os
import stat as stat_module
from pathlib import Path
from typing import Any, Iterable

from ..infrastructure.sorter_engine import (
    _atomic_write_json,
    _validated_state_document_path,
)
from ._constants import (
    LEGACY_RULES_MIGRATION_INBOX_DIR,
    LEGACY_RULES_MIGRATION_INBOX_PATTERN,
    LEGACY_RULES_MIGRATION_MAX_BYTES,
    LEGACY_RULES_QUARANTINE_PREFIX,
)
from .models import FileSorterError, KeywordRule, LegacyRulesMigration
from .paths import (
    _is_lexically_canonical_absolute,
    _link_or_reparse_status,
    is_absolute_destination,
    is_local_folder_name,
    normalize_match_text,
    normalize_text,
)
from .rules_io import _read_rules_document, get_rules_path


def _legacy_rules_migration_inbox_paths() -> list[Path]:
    """Return authenticated, regular legacy-rule snapshots from the updater."""

    inbox = LEGACY_RULES_MIGRATION_INBOX_DIR
    if not inbox.exists():
        return []
    if _link_or_reparse_status(inbox) is not False or not inbox.is_dir():
        return []
    try:
        candidates = list(inbox.iterdir())
    except OSError:
        return []

    source_priority = {
        "keyword_rules.json": 0,
        "keyword_rules.py": 1,
        ".file-sorter-rules.json": 2,
    }
    accepted: list[tuple[int, str, str, Path]] = []
    for candidate in candidates:
        match = LEGACY_RULES_MIGRATION_INBOX_PATTERN.fullmatch(candidate.name)
        if match is None or _link_or_reparse_status(candidate) is not False:
            continue
        try:
            candidate_stat = candidate.lstat()
            if (
                not stat_module.S_ISREG(candidate_stat.st_mode)
                or candidate_stat.st_size > LEGACY_RULES_MIGRATION_MAX_BYTES
            ):
                continue
            payload = candidate.read_bytes()
        except OSError:
            continue
        expected_digest = match.group("digest")
        if hashlib.sha256(payload).hexdigest() != expected_digest:
            continue
        source_name = match.group("source")
        accepted.append(
            (
                source_priority[source_name],
                candidate.name,
                expected_digest,
                candidate,
            )
        )
    accepted.sort(key=lambda item: (item[0], item[1]))
    selected: list[Path] = []
    seen_digests: set[str] = set()
    for _priority, _name, digest, candidate in accepted:
        if digest in seen_digests:
            continue
        seen_digests.add(digest)
        selected.append(candidate)
    return selected


def _legacy_rule_rejection(
    index: int,
    *,
    keyword: str = "",
    folder: str = "",
    reason: str,
) -> dict[str, Any]:
    return {
        "index": index,
        "keyword": keyword,
        "folder": folder,
        "reason": reason,
    }


def _migrate_legacy_rule_values(
    raw_rules: Iterable[Any],
    target: Path,
    *,
    require_existing_destination: bool,
) -> LegacyRulesMigration:
    migrated: list[KeywordRule] = []
    rejected: list[dict[str, Any]] = []
    converted_count = 0

    for index, item in enumerate(raw_rules, start=1):
        if not isinstance(item, dict):
            rejected.append(
                _legacy_rule_rejection(
                    index,
                    reason="rule-is-not-an-object",
                )
            )
            continue
        keyword = str(item.get("keyword", "")).strip()
        folder = str(item.get("folder", "")).strip()
        if not keyword or not normalize_match_text(keyword):
            rejected.append(
                _legacy_rule_rejection(
                    index,
                    keyword=keyword,
                    folder=folder,
                    reason="empty-keyword",
                )
            )
            continue

        if is_absolute_destination(folder):
            selected_destination = Path(folder).expanduser()
            if (
                not _is_lexically_canonical_absolute(selected_destination)
                or os.path.normcase(str(selected_destination.parent))
                != os.path.normcase(str(target))
            ):
                rejected.append(
                    _legacy_rule_rejection(
                        index,
                        keyword=keyword,
                        folder=folder,
                        reason="absolute-destination-outside-target",
                    )
                )
                continue
            try:
                canonical_destination = selected_destination.resolve(strict=False)
            except (OSError, RuntimeError):
                rejected.append(
                    _legacy_rule_rejection(
                        index,
                        keyword=keyword,
                        folder=folder,
                        reason="absolute-destination-unresolvable",
                    )
                )
                continue
            if canonical_destination.parent != target:
                rejected.append(
                    _legacy_rule_rejection(
                        index,
                        keyword=keyword,
                        folder=folder,
                        reason="absolute-destination-outside-target",
                    )
                )
                continue
            try:
                destination_stat = selected_destination.lstat()
            except FileNotFoundError:
                link_status = False
            except OSError:
                rejected.append(
                    _legacy_rule_rejection(
                        index,
                        keyword=keyword,
                        folder=folder,
                        reason="absolute-destination-unavailable",
                    )
                )
                continue
            else:
                attributes = int(
                    getattr(destination_stat, "st_file_attributes", 0) or 0
                )
                link_status = stat_module.S_ISLNK(
                    destination_stat.st_mode
                ) or bool(attributes & 0x400)
            if link_status:
                rejected.append(
                    _legacy_rule_rejection(
                        index,
                        keyword=keyword,
                        folder=folder,
                        reason="absolute-destination-is-link-or-reparse",
                    )
                )
                continue
            folder = canonical_destination.name
            converted_count += 1

        if not is_local_folder_name(folder):
            rejected.append(
                _legacy_rule_rejection(
                    index,
                    keyword=keyword,
                    folder=folder,
                    reason="destination-is-not-a-direct-child-name",
                )
            )
            continue
        if require_existing_destination:
            selected_destination = target / folder
            link_status = _link_or_reparse_status(selected_destination)
            if (
                link_status is not False
                or not selected_destination.is_dir()
            ):
                rejected.append(
                    _legacy_rule_rejection(
                        index,
                        keyword=keyword,
                        folder=folder,
                        reason="destination-is-not-an-existing-safe-direct-child",
                    )
                )
                continue
        migrated.append(KeywordRule(keyword=keyword, folder=folder))

    return LegacyRulesMigration(
        rules=tuple(migrated),
        rejected=tuple(rejected),
        converted_count=converted_count,
    )


def _write_legacy_rules_quarantine(
    configured_path: Path,
    *,
    source_path: Path,
    target: Path,
    migration: LegacyRulesMigration,
    state_root: str | Path | None,
) -> Path | None:
    if not migration.rejected:
        return None
    document = {
        "schema_version": 1,
        "source_path": str(source_path.resolve(strict=False)),
        "target_dir": str(target),
        "automatic_classification_enabled": False,
        "converted_rule_count": migration.converted_count,
        "rejected_rule_count": len(migration.rejected),
        "rejected_rules": [dict(item) for item in migration.rejected],
    }
    document_digest = hashlib.sha256(
        json.dumps(
            document,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:20]
    quarantine_path = configured_path.parent / (
        f"{LEGACY_RULES_QUARANTINE_PREFIX}{document_digest}.json"
    )
    try:
        _validated_state_document_path(
            quarantine_path,
            state_root=state_root,
            category="profiles",
            relative_parts=2,
            require_exists=False,
        )
        quarantine_path.parent.mkdir(parents=True, exist_ok=True)
        if quarantine_path.exists():
            existing = json.loads(quarantine_path.read_text(encoding="utf-8"))
            if existing != document:
                raise OSError("quarantine digest collision")
        else:
            _atomic_write_json(quarantine_path, document)
    except (OSError, json.JSONDecodeError) as error:
        raise FileSorterError(f"無法保存被隔離的舊規則：{error}") from error
    return quarantine_path


def _legacy_rules_for_migration(
    target: Path,
    configured_path: Path,
    *,
    state_root: str | Path | None,
) -> tuple[list[KeywordRule], Path, int]:
    legacy_path = get_rules_path()
    source_paths = _legacy_rules_migration_inbox_paths()
    inbox_paths = set(source_paths)
    if legacy_path not in inbox_paths:
        source_paths.append(legacy_path)

    migrated_rules: list[KeywordRule] = []
    seen_keywords: dict[str, str] = {}
    migrated_from: Path | None = None
    rejected_count = 0
    for source_path in source_paths:
        try:
            raw_rules = _read_rules_document(source_path)
        except FileSorterError:
            if source_path not in inbox_paths:
                raise
            migration = LegacyRulesMigration(
                rules=(),
                rejected=(
                    _legacy_rule_rejection(
                        0,
                        reason="rules-document-unreadable",
                    ),
                ),
            )
        else:
            migration = _migrate_legacy_rule_values(
                raw_rules,
                target,
                require_existing_destination=True,
            )

        accepted_rules: list[KeywordRule] = []
        additional_rejections = list(migration.rejected)
        for rule in migration.rules:
            normalized_keyword = normalize_match_text(rule.keyword)
            normalized_folder = normalize_text(rule.folder)
            previous_folder = seen_keywords.get(normalized_keyword)
            if previous_folder is None:
                seen_keywords[normalized_keyword] = normalized_folder
                accepted_rules.append(rule)
                continue
            if previous_folder != normalized_folder:
                additional_rejections.append(
                    _legacy_rule_rejection(
                        0,
                        keyword=rule.keyword,
                        folder=rule.folder,
                        reason="duplicate-keyword-destination-conflict",
                    )
                )

        source_migration = LegacyRulesMigration(
            rules=tuple(accepted_rules),
            rejected=tuple(additional_rejections),
            converted_count=migration.converted_count,
        )
        _write_legacy_rules_quarantine(
            configured_path,
            source_path=source_path,
            target=target,
            migration=source_migration,
            state_root=state_root,
        )
        rejected_count += len(source_migration.rejected)
        if accepted_rules and migrated_from is None:
            migrated_from = source_path
        migrated_rules.extend(accepted_rules)

    return migrated_rules, migrated_from or legacy_path, rejected_count
