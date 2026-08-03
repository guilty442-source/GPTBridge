"""自動化檔案管理工具。"""

from __future__ import annotations

import argparse
import ast
import fnmatch
import hashlib
import json
import os
import re
import stat as stat_module
import sys
import threading
import unicodedata
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

TOOL_ROOT = Path(__file__).resolve().parents[5]

from ..infrastructure.cleanup import (
    CleanupError,
    DEFAULT_ANALYSIS_SPEED,
    DEFAULT_SIMILAR_VIDEO_THRESHOLD,
    find_exact_duplicate_candidates,
    print_progress_event,
    recycle_exact_duplicate_candidates,
    run_cleanup_scan,
)
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
    _TargetDirectoryLock,
    _atomic_write_json,
    _state_category_root,
    _utc_now,
    _validated_state_document_path,
)


RULES_VARIABLE_NAME = "KEYWORD_RULES"
PACKAGED_JSON_RULES_FILE_PATH = TOOL_ROOT / "src" / "keyword_rules.json"
_settings_root_override = str(
    os.environ.get("GPTBRIDGE_TOOL_SETTINGS_ROOT") or ""
).strip()
TOOL_SETTINGS_ROOT = (
    Path(_settings_root_override).resolve()
    if _settings_root_override
    else TOOL_ROOT / "runtime" / "settings"
)
try:
    TOOL_SETTINGS_ROOT.relative_to(TOOL_ROOT)
except ValueError as error:
    raise RuntimeError(
        "File Sorter settings must stay inside its own tool folder"
    ) from error
JSON_RULES_FILE_PATH = TOOL_SETTINGS_ROOT / "keyword_rules.json"
PY_RULES_FILE_PATH = TOOL_ROOT / "src" / "keyword_rules.py"
RULES_FILE_PATH = JSON_RULES_FILE_PATH
LEGACY_RULES_FILE_NAME = ".file-sorter-rules.json"
LEGACY_RULES_QUARANTINE_PREFIX = "legacy-rules-quarantine-"
LEGACY_RULES_MIGRATION_INBOX_DIR = (
    TOOL_ROOT / "runtime" / "legacy-rule-migration-inbox"
)
LEGACY_RULES_MIGRATION_INBOX_PATTERN = re.compile(
    r"^(?P<digest>[0-9a-f]{64})-"
    r"(?P<source>keyword_rules\.json|keyword_rules\.py|\.file-sorter-rules\.json)$"
)
LEGACY_RULES_MIGRATION_MAX_BYTES = 8 * 1024 * 1024
FOLDERS_JSON_PREFIX = "FILE_SORTER_FOLDERS_JSON="
SOURCE_FILES_JSON_PREFIX = "FILE_SORTER_SOURCE_FILES_JSON="
_BACKGROUND_OBSERVATIONS_LOCK = threading.Lock()
_BACKGROUND_OBSERVATIONS: dict[
    tuple[str, str],
    dict[str, tuple[int, int]],
] = {}
_BACKGROUND_DUPLICATE_OBSERVATIONS: dict[
    tuple[str, str],
    dict[str, tuple[int, int, str, str]],
] = {}


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
    transaction_id: str | None = None
    journal_path: str | None = None
    plan: dict[str, Any] | None = None


@dataclass
class KeywordUpsertResult:
    added: list[KeywordRule] = field(default_factory=list)
    updated: list[KeywordRule] = field(default_factory=list)
    unchanged: list[KeywordRule] = field(default_factory=list)


@dataclass(frozen=True)
class LegacyRulesMigration:
    rules: tuple[KeywordRule, ...]
    rejected: tuple[dict[str, Any], ...]
    converted_count: int = 0


def normalize_text(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold().strip()


def normalize_match_text(value: str) -> str:
    return re.sub(r"[\s_]+", " ", normalize_text(value))


def _link_or_reparse_status(path: Path) -> bool | None:
    """Return link/reparse status; inaccessible paths fail closed as ``None``."""

    try:
        value = path.lstat()
    except OSError:
        return None
    attributes = int(getattr(value, "st_file_attributes", 0) or 0)
    return stat_module.S_ISLNK(value.st_mode) or bool(attributes & 0x400)


def resolve_target_dir(target_dir: str | Path) -> Path:
    selected = Path(target_dir).expanduser()
    if not _is_lexically_canonical_absolute(selected):
        raise FileSorterError(f"目標資料夾必須是正規化的絕對路徑：{selected}")
    link_status = _link_or_reparse_status(selected)
    if link_status is None:
        raise FileSorterError(f"無法安全存取目標資料夾：{selected}")
    if link_status:
        raise FileSorterError("目標資料夾不得是符號連結、junction 或 reparse point。")
    try:
        target = selected.resolve(strict=True)
    except OSError as error:
        raise FileSorterError(f"無法解析目標資料夾：{selected}；{error}") from error
    if os.path.normcase(str(selected)) != os.path.normcase(str(target)):
        raise FileSorterError(f"目標資料夾必須使用正規路徑：{selected}")
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


def _is_lexically_canonical_absolute(path: Path) -> bool:
    if not path.is_absolute():
        return False
    normalized = Path(os.path.abspath(os.path.normpath(str(path))))
    return os.path.normcase(str(path)) == os.path.normcase(str(normalized))


def _profile_id_for_canonical_target(
    target: Path,
    profile: str | None,
) -> str:
    normalized = os.path.normcase(str(target))
    target_digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
    if not profile:
        return f"target-{target_digest}"
    slug = re.sub(r"[^a-zA-Z0-9_.-]+", "-", profile.strip()).strip(".-")
    if not slug:
        raise FileSorterError("Profile name must contain a letter or number.")
    name_digest = hashlib.sha256(profile.strip().encode("utf-8")).hexdigest()[:10]
    return f"{slug[:37]}-{name_digest}-{target_digest}"


def resolve_destination_dir(
    target_dir: Path,
    folder: str,
) -> Path:
    folder_name = folder.strip()
    if is_absolute_destination(folder_name):
        raise FileSorterError("分類目的地不得離開目前選定的資料夾。")
    if (
        not folder_name
        or folder_name in {".", ".."}
        or Path(folder_name).name != folder_name
        or "/" in folder_name
        or "\\" in folder_name
    ):
        raise FileSorterError("分類資料夾必須是目標目錄內的單層資料夾名稱。")

    selected_destination = target_dir / folder_name
    link_status = _link_or_reparse_status(selected_destination)
    if link_status is None:
        raise FileSorterError(f"分類資料夾不存在或無法安全存取：{folder_name}")
    if link_status:
        raise FileSorterError("分類目的地不得是符號連結、junction 或 reparse point。")
    destination = selected_destination.resolve()
    if destination.parent != target_dir:
        raise FileSorterError("分類資料夾不可離開目標目錄。")
    if destination.exists() and not destination.is_dir():
        raise FileSorterError(f"分類目的地不是資料夾：{folder_name}")
    if not destination.is_dir():
        raise FileSorterError(f"分類資料夾不存在：{folder_name}；禁止自動建立子資料夾。")
    return destination


def destination_rule_value(folder: str, destination: Path) -> str:
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
        if _link_or_reparse_status(item) is False
        and item.is_dir()
        and not item.name.startswith(".")
    ]


def list_source_files(target_dir: str | Path) -> list[dict[str, int | str]]:
    target = resolve_target_dir(target_dir)
    files: list[dict[str, int | str]] = []
    for item in sorted(target.iterdir(), key=lambda path: normalize_text(path.name)):
        if (
            _link_or_reparse_status(item) is not False
            or not item.is_file()
            or item.name == LEGACY_RULES_FILE_NAME
        ):
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


def _migrate_existing_profile_rules(
    configured_path: Path,
    *,
    target: Path,
    state_root: str | Path | None,
    profile: str | None,
) -> ProfileSnapshot | None:
    """Recover pre-boundary profiles without re-enabling automatic moves."""

    try:
        lock_path = configured_path.with_suffix(".lock")
        for candidate in (configured_path, lock_path):
            _validated_state_document_path(
                candidate,
                state_root=state_root,
                category="profiles",
                relative_parts=2,
                require_exists=candidate == configured_path,
            )
        with _ExclusiveFileLock(lock_path):
            _validated_state_document_path(
                configured_path,
                state_root=state_root,
                category="profiles",
                relative_parts=2,
                require_exists=True,
            )
            value = json.loads(configured_path.read_text(encoding="utf-8"))
            if not isinstance(value, dict) or not isinstance(value.get("rules"), list):
                return None

            stored_target_text = str(
                value.get("target_dir") or value.get("target") or ""
            ).strip()
            stored_profile_id = str(
                value.get("profile_id") or configured_path.parent.name
            ).strip()
            stored_profile_name = (
                str(value["profile_name"])
                if value.get("profile_name") is not None
                else None
            )
            raw_target = Path(stored_target_text).expanduser()
            if (
                not stored_target_text
                or not _is_lexically_canonical_absolute(raw_target)
                or os.path.normcase(str(raw_target))
                != os.path.normcase(str(target))
                or stored_profile_name != profile
                or configured_path.name != "profile.json"
                or stored_profile_id != configured_path.parent.name
                or stored_profile_id
                != _profile_id_for_canonical_target(raw_target, stored_profile_name)
            ):
                return None

            migration = _migrate_legacy_rule_values(
                value["rules"],
                target,
                require_existing_destination=False,
            )
            if not migration.rejected and migration.converted_count == 0:
                return None

            source_digest = hashlib.sha256(
                json.dumps(
                    value,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            quarantine_path = configured_path.parent / (
                f"{LEGACY_RULES_QUARANTINE_PREFIX}"
                f"{source_digest[:20]}-profile.json"
            )
            _validated_state_document_path(
                quarantine_path,
                state_root=state_root,
                category="profiles",
                relative_parts=2,
                require_exists=False,
            )
            if quarantine_path.exists():
                existing = json.loads(quarantine_path.read_text(encoding="utf-8"))
                if existing != value:
                    raise OSError("profile quarantine digest collision")
            else:
                _atomic_write_json(quarantine_path, value)

            try:
                old_revision = max(0, int(value.get("revision", 0)))
            except (TypeError, ValueError):
                old_revision = 0
            try:
                quiet_seconds = max(
                    0.0,
                    float(value.get("quiet_seconds", DEFAULT_QUIET_SECONDS)),
                )
            except (TypeError, ValueError):
                quiet_seconds = DEFAULT_QUIET_SECONDS
            raw_include = value.get("include")
            raw_exclude = value.get("exclude")
            migrated_document = {
                **value,
                "schema_version": 1,
                "profile_id": stored_profile_id,
                "profile_name": stored_profile_name,
                "target": str(target),
                "target_dir": str(target),
                "revision": old_revision + 1,
                "enabled": False,
                "quiet_seconds": quiet_seconds,
                "include": (
                    [str(item) for item in raw_include if str(item).strip()]
                    if isinstance(raw_include, list)
                    else ["*"]
                ),
                "exclude": (
                    [str(item) for item in raw_exclude if str(item).strip()]
                    if isinstance(raw_exclude, list)
                    else []
                ),
                "rules": [
                    {"keyword": rule.keyword, "folder": rule.folder}
                    for rule in migration.rules
                ],
                "migrated_from": str(quarantine_path),
                "migration_required_review": True,
                "migration_rejected_rule_count": len(migration.rejected),
            }
            _atomic_write_json(configured_path, migrated_document)
    except (
        OSError,
        json.JSONDecodeError,
        RuleConflictError,
        SorterV2Error,
        TypeError,
        ValueError,
    ) as error:
        raise FileSorterError(f"無法安全遷移舊版 profile 規則：{error}") from error

    try:
        return load_profile(
            target,
            state_root=state_root,
            profile=profile,
        )
    except SorterV2Error as error:
        raise FileSorterError(f"無法讀回已遷移的 profile：{error}") from error


def _load_profile_snapshot(
    target_dir: str | Path,
    *,
    state_root: str | Path | None = None,
    profile: str | None = None,
) -> ProfileSnapshot:
    target = resolve_target_dir(target_dir)
    configured_path = profile_path(
        target,
        state_root=state_root,
        profile=profile,
    )
    legacy_rejected_count = 0
    if configured_path.exists():
        migrated_snapshot = _migrate_existing_profile_rules(
            configured_path,
            target=target,
            state_root=state_root,
            profile=profile,
        )
        if migrated_snapshot is not None:
            return migrated_snapshot
        legacy_rules: list[KeywordRule] = []
        legacy_path: Path | None = None
    else:
        legacy_rules, legacy_path, legacy_rejected_count = (
            _legacy_rules_for_migration(
                target,
                configured_path,
                state_root=state_root,
            )
        )
    try:
        snapshot = load_profile(
            target,
            state_root=state_root,
            profile=profile,
            legacy_rules=(
                {"keyword": rule.keyword, "folder": rule.folder}
                for rule in legacy_rules
                if rule.source == "custom"
            ),
            legacy_path=legacy_path,
        )
    except SorterV2Error as error:
        raise FileSorterError(str(error)) from error
    if legacy_rejected_count <= 0:
        return snapshot

    try:
        lock_path = configured_path.with_suffix(".lock")
        for candidate in (configured_path, lock_path):
            _validated_state_document_path(
                candidate,
                state_root=state_root,
                category="profiles",
                relative_parts=2,
                require_exists=candidate == configured_path,
            )
        with _ExclusiveFileLock(lock_path):
            _validated_state_document_path(
                configured_path,
                state_root=state_root,
                category="profiles",
                relative_parts=2,
                require_exists=True,
            )
            value = json.loads(configured_path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise FileSorterError("New profile document is not an object.")
            stored_target = str(
                value.get("target_dir") or value.get("target") or ""
            ).strip()
            stored_profile_id = str(value.get("profile_id") or "").strip()
            if (
                stored_target != str(target)
                or stored_profile_id != snapshot.profile_id
                or configured_path.parent.name != snapshot.profile_id
                or value.get("profile_name") != profile
            ):
                raise FileSorterError(
                    "New profile identity changed during legacy migration."
                )
            try:
                old_revision = max(0, int(value.get("revision", 0)))
            except (TypeError, ValueError):
                old_revision = 0
            _atomic_write_json(
                configured_path,
                {
                    **value,
                    "revision": old_revision + 1,
                    "enabled": False,
                    "migration_required_review": True,
                    "migration_rejected_rule_count": legacy_rejected_count,
                },
            )
    except (OSError, json.JSONDecodeError, SorterV2Error) as error:
        raise FileSorterError(
            f"Cannot persist legacy migration review state: {error}"
        ) from error
    try:
        return load_profile(
            target,
            state_root=state_root,
            profile=profile,
        )
    except SorterV2Error as error:
        raise FileSorterError(str(error)) from error


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
) -> ProfileSnapshot | None:
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


def _read_rules_and_revision(
    target_dir: str | Path,
    *,
    state_root: str | Path | None = None,
    profile: str | None = None,
) -> tuple[list[KeywordRule], int | None]:
    if _uses_explicit_legacy_rules_path():
        return read_custom_rules(), None
    snapshot = _load_profile_snapshot(
        target_dir,
        state_root=state_root,
        profile=profile,
    )
    return (
        read_custom_rules(target_dir, state_root=state_root, profile=profile),
        snapshot.revision,
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


def _effective_state_root(
    state_root: str | Path | None,
) -> str | Path | None:
    if state_root is not None or not _uses_explicit_legacy_rules_path():
        return state_root
    return get_rules_path().parent / ".file-sorter-v1-state"


def _profile_snapshot_for_plan(
    target: Path,
    *,
    state_root: str | Path | None,
    profile: str | None,
) -> ProfileSnapshot:
    if not _uses_explicit_legacy_rules_path():
        return _load_profile_snapshot(
            target,
            state_root=state_root,
            profile=profile,
        )
    return ProfileSnapshot(
        profile_id=profile_path(
            target,
            state_root=_effective_state_root(state_root),
            profile=profile,
        ).parent.name,
        profile_name=profile,
        target_dir=str(target),
        revision=0,
        enabled=True,
        duplicate_trash_enabled=False,
        quiet_seconds=DEFAULT_QUIET_SECONDS,
        include=("*",),
        exclude=(),
        rules=tuple(
            {"keyword": rule.keyword, "folder": rule.folder}
            for rule in read_custom_rules()
        ),
        path=get_rules_path(),
    )


def _matches_profile_patterns(
    name: str,
    *,
    include: Iterable[str],
    exclude: Iterable[str],
) -> bool:
    normalized_name = normalize_text(name)
    included = any(
        fnmatch.fnmatchcase(normalized_name, normalize_text(pattern))
        for pattern in include
    )
    excluded = any(
        fnmatch.fnmatchcase(normalized_name, normalize_text(pattern))
        for pattern in exclude
    )
    return included and not excluded


def _planned_unique_destination(
    destination_dir: Path,
    file_name: str,
    reserved: set[str],
) -> Path:
    source_name = Path(file_name)
    for index in range(100_000):
        candidate = (
            destination_dir / file_name
            if index == 0
            else destination_dir / f"{source_name.stem}_{index}{source_name.suffix}"
        )
        key = os.path.normcase(str(candidate.resolve(strict=False)))
        if key not in reserved and not candidate.exists():
            reserved.add(key)
            return candidate
    raise FileSorterError(f"Unable to allocate a unique destination for {file_name}")


def preview_organize_files(
    target_dir: str | Path,
    *,
    quiet_seconds: float = DEFAULT_QUIET_SECONDS,
    state_root: str | Path | None = None,
    profile: str | None = None,
    persist: bool = True,
) -> OrganizePlan:
    target = resolve_target_dir(target_dir)
    effective_root = _effective_state_root(state_root)
    snapshot = _profile_snapshot_for_plan(
        target,
        state_root=effective_root,
        profile=profile,
    )
    rules = build_keyword_rules(
        target,
        state_root=effective_root,
        profile=profile,
    )
    operations: list[PlanOperation] = []
    skipped: list[SkippedFile] = []
    reserved: set[str] = set()
    files = [
        item
        for item in sorted(target.iterdir(), key=lambda path: normalize_text(path.name))
        if item.is_file() and item.name != LEGACY_RULES_FILE_NAME
    ]
    for item in files:
        if not _matches_profile_patterns(
            item.name,
            include=snapshot.include,
            exclude=snapshot.exclude,
        ):
            skipped.append(
                SkippedFile(
                    source=str(item),
                    category="filtered",
                    reason="profile-pattern",
                )
            )
            continue
        stability = check_file_stability(
            item,
            quiet_seconds=max(0.0, quiet_seconds),
        )
        if not stability.stable or stability.fingerprint is None:
            skipped.append(
                SkippedFile(
                    source=str(item),
                    category="unstable",
                    reason=stability.reason or "unknown",
                )
            )
            continue
        matched_rule = next(
            (rule for rule in rules if keyword_matches(item.stem, rule.keyword)),
            None,
        )
        if matched_rule is None:
            skipped.append(
                SkippedFile(
                    source=str(item),
                    category="unmatched",
                    reason="no-keyword-rule",
                )
            )
            continue
        destination_dir = resolve_destination_dir(target, matched_rule.folder)
        destination = _planned_unique_destination(
            destination_dir,
            item.name,
            reserved,
        )
        operations.append(
            PlanOperation(
                operation_id=str(uuid.uuid4()),
                source=str(item),
                destination=str(destination),
                keyword=matched_rule.keyword,
                folder=matched_rule.folder,
                rule_source=matched_rule.source,
                source_size=stability.fingerprint.size,
                source_mtime_ns=stability.fingerprint.mtime_ns,
                transfer=(
                    "same-volume"
                    if same_volume(item, destination_dir)
                    else "cross-volume"
                ),
            )
        )
    plan = new_plan(
        target,
        profile_id=snapshot.profile_id,
        rules_revision=snapshot.revision,
        quiet_seconds=quiet_seconds,
        operations=operations,
        skipped=skipped,
    )
    if persist:
        try:
            save_plan(plan, state_root=effective_root)
        except SorterV2Error as error:
            raise FileSorterError(str(error)) from error
    return plan


def apply_organize_plan(
    plan_id: str,
    *,
    target_dir: str | Path | None = None,
    state_root: str | Path | None = None,
    profile: str | None = None,
) -> dict[str, Any]:
    effective_root = _effective_state_root(state_root)
    try:
        plan = load_plan(plan_id, state_root=effective_root)
    except SorterV2Error as error:
        raise FileSorterError(str(error)) from error
    if target_dir is not None:
        target = resolve_target_dir(target_dir)
        if Path(plan.target_dir).resolve() != target:
            raise FileSorterError(
                f"Plan target mismatch: {plan.target_dir} != {target}"
            )
    validate_profile = None
    if not _uses_explicit_legacy_rules_path():
        def validate_profile() -> None:
            """Revalidate rules while execute_plan holds the target lock."""

            snapshot = next(
                (
                    item
                    for item in list_profiles(state_root=effective_root)
                    if item.profile_id == plan.profile_id
                    and Path(item.target_dir).resolve()
                    == Path(plan.target_dir).resolve()
                ),
                None,
            )
            if snapshot is None:
                raise FileSorterError(
                    "The profile used by this plan no longer exists."
                )
            if profile is not None:
                selected_path = profile_path(
                    plan.target_dir,
                    state_root=effective_root,
                    profile=profile,
                )
                if selected_path.parent.name != snapshot.profile_id:
                    raise FileSorterError(
                        "Plan profile no longer matches the selected profile."
                    )
            if snapshot.revision != plan.rules_revision:
                raise FileSorterError(
                    "Rules changed after preview; create a new plan before applying."
                )
            current_rules = build_keyword_rules(
                plan.target_dir,
                state_root=effective_root,
                profile=snapshot.profile_name,
            )
            allowed_rules = {
                (
                    normalize_match_text(rule.keyword),
                    os.path.normcase(
                        str(
                            resolve_destination_dir(
                                Path(plan.target_dir).resolve(),
                                rule.folder,
                            )
                        )
                    ),
                )
                for rule in current_rules
            }
            for operation in plan.operations:
                try:
                    operation_destination = os.path.normcase(
                        str(
                            resolve_destination_dir(
                                Path(plan.target_dir).resolve(),
                                operation.folder,
                            )
                        )
                    )
                except FileSorterError as error:
                    raise FileSorterError(
                        "A destination rule in the plan is no longer valid."
                    ) from error
                if (
                    normalize_match_text(operation.keyword),
                    operation_destination,
                ) not in allowed_rules:
                    raise FileSorterError(
                        "A plan operation no longer matches the active profile rules."
                    )
    try:
        return execute_plan(
            plan,
            state_root=effective_root,
            pre_execute_validate=validate_profile,
        )
    except SorterV2Error as error:
        raise FileSorterError(str(error)) from error


def organize_files(
    target_dir: str | Path,
    *,
    quiet_seconds: float = 0.0,
    dry_run: bool = False,
    state_root: str | Path | None = None,
    profile: str | None = None,
) -> OrganizeResult:
    """Compatibility API backed by a V2 preview and durable transaction."""

    target = resolve_target_dir(target_dir)
    effective_root = _effective_state_root(state_root)
    rules = build_keyword_rules(
        target,
        state_root=effective_root,
        profile=profile,
    )
    result = OrganizeResult(rules=rules)
    if not rules:
        result.errors.append("No usable keyword rules or destination folders were found.")
        return result
    plan = preview_organize_files(
        target,
        quiet_seconds=quiet_seconds,
        state_root=effective_root,
        profile=profile,
        persist=True,
    )
    result.plan = plan.to_dict()
    result.unmatched_count = sum(
        1 for item in plan.skipped if item.category == "unmatched"
    )
    for item in plan.skipped:
        if item.category == "unstable":
            result.warnings.append(
                f"Skipped unstable file {Path(item.source).name}: {item.reason}"
            )
    if dry_run:
        return result
    try:
        execution = execute_plan(plan, state_root=effective_root)
    except SorterV2Error as error:
        result.errors.append(str(error))
        return result
    result.moved_count = int(execution["moved_count"])
    result.errors.extend(str(item) for item in execution["errors"])
    result.transaction_id = str(execution["transaction_id"])
    result.journal_path = str(execution["journal_path"])
    return result


def configure_profile_enabled(
    target_dir: str | Path,
    enabled: bool,
    *,
    state_root: str | Path | None = None,
    profile: str | None = None,
) -> ProfileSnapshot:
    if _uses_explicit_legacy_rules_path():
        raise FileSorterError("Profiles require the V2 user state repository.")
    snapshot = _load_profile_snapshot(
        target_dir,
        state_root=state_root,
        profile=profile,
    )
    try:
        return save_profile(
            snapshot,
            enabled=enabled,
            acknowledge_migration_review=(
                enabled and snapshot.migration_required_review
            ),
        )
    except SorterV2Error as error:
        raise FileSorterError(str(error)) from error


def configure_duplicate_trash_enabled(
    target_dir: str | Path,
    enabled: bool,
    *,
    state_root: str | Path | None = None,
    profile: str | None = None,
) -> ProfileSnapshot:
    """Explicitly opt a profile into or out of recoverable duplicate cleanup."""

    if _uses_explicit_legacy_rules_path():
        raise FileSorterError("Duplicate recycling requires the V2 user state repository.")
    snapshot = _load_profile_snapshot(
        target_dir,
        state_root=state_root,
        profile=profile,
    )
    try:
        return save_profile(
            snapshot,
            duplicate_trash_enabled=enabled,
        )
    except SorterV2Error as error:
        raise FileSorterError(str(error)) from error


def _migrate_legacy_profiles_for_automation(
    state_root: str | Path | None,
) -> list[dict[str, Any]]:
    reports: list[dict[str, Any]] = []
    try:
        profiles_dir = _state_category_root(state_root, "profiles")
    except SorterV2Error:
        return reports
    if not profiles_dir.is_dir():
        return reports

    for configured_path in profiles_dir.glob("*/profile.json"):
        try:
            _validated_state_document_path(
                configured_path,
                state_root=state_root,
                category="profiles",
                relative_parts=2,
                require_exists=True,
            )
        except SorterV2Error:
            continue
        try:
            value = json.loads(configured_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(value, dict) or not isinstance(value.get("rules"), list):
            continue
        target_text = str(
            value.get("target_dir") or value.get("target") or ""
        ).strip()
        profile_name = (
            str(value["profile_name"])
            if value.get("profile_name") is not None
            else None
        )
        profile_id = str(
            value.get("profile_id") or configured_path.parent.name
        ).strip()
        raw_target = Path(target_text).expanduser()
        try:
            identity_valid = (
                configured_path.name == "profile.json"
                and _is_lexically_canonical_absolute(raw_target)
                and profile_id == configured_path.parent.name
                and profile_id
                == _profile_id_for_canonical_target(raw_target, profile_name)
            )
        except FileSorterError:
            identity_valid = False
        if not identity_valid:
            reports.append(
                {
                    "ok": False,
                    "profile_id": profile_id,
                    "target_dir": target_text,
                    "moved_count": 0,
                    "errors": [
                        "Profile identity is invalid; target was not accessed."
                    ],
                    "warnings": [],
                    "migration_required_review": True,
                }
            )
            continue
        try:
            target = resolve_target_dir(target_text)
            migrated = _migrate_existing_profile_rules(
                configured_path,
                target=target,
                state_root=state_root,
                profile=profile_name,
            )
        except FileSorterError as error:
            reports.append(
                {
                    "ok": False,
                    "profile_id": profile_id,
                    "target_dir": target_text,
                    "moved_count": 0,
                    "errors": [str(error)],
                    "warnings": [],
                    "migration_required_review": True,
                }
            )
            continue
        if migrated is None:
            if value.get("migration_required_review") is True:
                reports.append(
                    {
                        "ok": True,
                        "profile_id": profile_id,
                        "target_dir": target_text,
                        "moved_count": 0,
                        "errors": [],
                        "warnings": [
                            "Legacy rule migration is waiting for user review; "
                            "automatic classification remains disabled."
                        ],
                        "migration_required_review": True,
                    }
                )
            continue
        reports.append(
            {
                "ok": True,
                "profile_id": migrated.profile_id,
                "target_dir": migrated.target_dir,
                "moved_count": 0,
                "errors": [],
                "warnings": [
                    "Legacy destination rules were quarantined; "
                    "automatic classification remains disabled pending review."
                ],
                "migration_required_review": True,
            }
        )
    return reports


def _record_duplicate_recycle_result(
    result: dict[str, Any],
    *,
    profile_id: str,
    state_root: str | Path | None,
) -> str | None:
    if int(result.get("recycled_count", 0)) <= 0:
        return None
    transaction_id = str(uuid.uuid4())
    path = (
        resolve_state_root(state_root)
        / "recycle-journals"
        / f"{transaction_id}.json"
    )
    _validated_state_document_path(
        path,
        state_root=state_root,
        category="recycle-journals",
        relative_parts=1,
        require_exists=False,
    )
    _atomic_write_json(
        path,
        {
            "schema_version": 1,
            "type": "file-sorter-duplicate-recycle-journal",
            "transaction_id": transaction_id,
            "profile_id": profile_id,
            "created_at": _utc_now(),
            **result,
        },
    )
    return transaction_id


def _run_profile_duplicate_recycle_once(
    snapshot: ProfileSnapshot,
    *,
    effective_root: str | Path | None,
    observation_key: tuple[str, str],
) -> dict[str, Any]:
    candidates = find_exact_duplicate_candidates(
        snapshot.target_dir,
        quiet_seconds=snapshot.quiet_seconds,
    )
    current_observations = {
        str(item["path"]): (
            int(item["size"]),
            int(item["mtime_ns"]),
            str(item["sha256"]),
            str(item["keep_path"]),
        )
        for item in candidates
    }
    with _BACKGROUND_OBSERVATIONS_LOCK:
        previous = _BACKGROUND_DUPLICATE_OBSERVATIONS.get(observation_key, {})
        _BACKGROUND_DUPLICATE_OBSERVATIONS[observation_key] = current_observations
    ready = [
        item
        for item in candidates
        if previous.get(str(item["path"]))
        == current_observations[str(item["path"])]
    ]
    result: dict[str, Any] = {
        "ok": True,
        "recycled_count": 0,
        "recycled": [],
        "errors": [],
    }
    if ready:
        with _TargetDirectoryLock(snapshot.target_dir):
            result = recycle_exact_duplicate_candidates(snapshot.target_dir, ready)
    transaction_id = _record_duplicate_recycle_result(
        result,
        profile_id=snapshot.profile_id,
        state_root=effective_root,
    )
    return {
        "recycled_count": int(result.get("recycled_count", 0)),
        "duplicate_waiting_for_second_observation_count": (
            len(current_observations) - len(ready)
        ),
        "duplicate_errors": [str(item) for item in result.get("errors", [])],
        "recycle_transaction_id": transaction_id,
    }


def _run_profile_classification_once(
    snapshot: ProfileSnapshot,
    *,
    effective_root: str | Path | None,
    observation_key: tuple[str, str],
) -> dict[str, Any]:
    plan = preview_organize_files(
        snapshot.target_dir,
        quiet_seconds=snapshot.quiet_seconds,
        state_root=effective_root,
        profile=snapshot.profile_name,
        persist=False,
    )
    current_observations = {
        operation.source: (
            operation.source_size,
            operation.source_mtime_ns,
        )
        for operation in plan.operations
    }
    with _BACKGROUND_OBSERVATIONS_LOCK:
        previous_observations = _BACKGROUND_OBSERVATIONS.get(observation_key, {})
        _BACKGROUND_OBSERVATIONS[observation_key] = current_observations
    ready_sources = {
        source
        for source, fingerprint in current_observations.items()
        if previous_observations.get(source) == fingerprint
    }
    plan.operations = [
        operation
        for operation in plan.operations
        if operation.source in ready_sources
    ]
    warnings = [
        f"Skipped unstable file {Path(item.source).name}: {item.reason}"
        for item in plan.skipped
        if item.category == "unstable"
    ]
    transaction_id: str | None = None
    errors: list[str] = []
    moved_count = 0
    if plan.operations:
        save_plan(plan, state_root=effective_root)
        execution = apply_organize_plan(
            plan.plan_id,
            target_dir=snapshot.target_dir,
            state_root=effective_root,
            profile=snapshot.profile_name,
        )
        moved_count = int(execution["moved_count"])
        errors = [str(item) for item in execution["errors"]]
        transaction_id = str(execution["transaction_id"])
    return {
        "moved_count": moved_count,
        "unmatched_count": sum(
            1 for item in plan.skipped if item.category == "unmatched"
        ),
        "waiting_for_second_observation_count": (
            len(current_observations) - len(ready_sources)
        ),
        "classification_errors": errors,
        "warnings": warnings,
        "transaction_id": transaction_id,
    }


def run_enabled_profiles_once(
    *,
    state_root: str | Path | None = None,
) -> list[dict[str, Any]]:
    """Run explicitly enabled classification and duplicate-recycle profiles."""

    effective_root = _effective_state_root(state_root)
    reports = _migrate_legacy_profiles_for_automation(effective_root)
    root_key = str(resolve_state_root(effective_root))
    active_classification_keys: set[tuple[str, str]] = set()
    active_duplicate_keys: set[tuple[str, str]] = set()
    for snapshot in list_profiles(state_root=effective_root):
        if snapshot.migration_required_review:
            continue
        if not snapshot.enabled and not snapshot.duplicate_trash_enabled:
            continue
        observation_key = (root_key, snapshot.profile_id)
        report: dict[str, Any] = {
            "ok": True,
            "profile_id": snapshot.profile_id,
            "target_dir": snapshot.target_dir,
            "moved_count": 0,
            "recycled_count": 0,
            "unmatched_count": 0,
            "waiting_for_second_observation_count": 0,
            "duplicate_waiting_for_second_observation_count": 0,
            "errors": [],
            "warnings": [],
            "transaction_id": None,
            "recycle_transaction_id": None,
        }
        try:
            if snapshot.duplicate_trash_enabled:
                active_duplicate_keys.add(observation_key)
                duplicate_report = _run_profile_duplicate_recycle_once(
                    snapshot,
                    effective_root=effective_root,
                    observation_key=observation_key,
                )
                report.update(duplicate_report)
                report["errors"].extend(duplicate_report["duplicate_errors"])
            if snapshot.enabled:
                active_classification_keys.add(observation_key)
                classification_report = _run_profile_classification_once(
                    snapshot,
                    effective_root=effective_root,
                    observation_key=observation_key,
                )
                report.update(classification_report)
                report["errors"].extend(
                    classification_report["classification_errors"]
                )
            report["ok"] = not report["errors"]
        except (CleanupError, FileSorterError, SorterV2Error, OSError) as error:
            report["ok"] = False
            report["errors"].append(str(error))
        reports.append(report)

    with _BACKGROUND_OBSERVATIONS_LOCK:
        for observation_key in list(_BACKGROUND_OBSERVATIONS):
            if (
                observation_key[0] == root_key
                and observation_key not in active_classification_keys
            ):
                _BACKGROUND_OBSERVATIONS.pop(observation_key, None)
        for observation_key in list(_BACKGROUND_DUPLICATE_OBSERVATIONS):
            if (
                observation_key[0] == root_key
                and observation_key not in active_duplicate_keys
            ):
                _BACKGROUND_DUPLICATE_OBSERVATIONS.pop(observation_key, None)
    return reports


def scan_after_keyword_addition(
    target_dir: str | Path,
    *,
    state_root: str | Path | None = None,
) -> dict[str, Any] | None:
    """Immediately observe a target after at least one new rule is saved."""

    target = str(Path(target_dir).resolve())
    for report in run_enabled_profiles_once(state_root=state_root):
        if str(report.get("target_dir", "")) == target:
            return report
    return None


def enabled_profile_targets(
    *,
    state_root: str | Path | None = None,
) -> list[str]:
    """Return validated roots for enabled profiles used by realtime monitoring."""

    effective_root = _effective_state_root(state_root)
    targets: set[str] = set()
    for snapshot in list_profiles(state_root=effective_root):
        if (
            (not snapshot.enabled and not snapshot.duplicate_trash_enabled)
            or snapshot.migration_required_review
        ):
            continue
        try:
            target = resolve_target_dir(snapshot.target_dir)
        except FileSorterError:
            continue
        targets.add(str(target))
    return sorted(targets, key=os.path.normcase)


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
