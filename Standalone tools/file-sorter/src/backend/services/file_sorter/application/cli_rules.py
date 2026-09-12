"""Rules migration, reading, and writing for file sorter CLI."""

from __future__ import annotations

import ast
import json
import os
import re
import stat as stat_module
import unicodedata
from pathlib import Path
from typing import Any

from ..infrastructure.sorter_engine import (
    ProfileSnapshot,
    RuleConflictError,
    SorterV2Error,
    load_profile,
    profile_path,
    save_profile,
)
from ..infrastructure.sorter_engine import (
    _ExclusiveFileLock,
    _atomic_write_json,
    _validated_state_document_path,
)
from .cli_constants import (
    JSON_RULES_FILE_PATH,
    LEGACY_RULES_FILE_NAME,
    LEGACY_RULES_MIGRATION_INBOX_DIR,
    LEGACY_RULES_MIGRATION_INBOX_PATTERN,
    LEGACY_RULES_MIGRATION_MAX_BYTES,
    LEGACY_RULES_QUARANTINE_PREFIX,
    PACKAGED_JSON_RULES_FILE_PATH,
    PY_RULES_FILE_PATH,
    RULES_FILE_PATH,
    RULES_VARIABLE_NAME,
    TOOL_ROOT,
)
from .cli_models import FileSorterError, KeywordRule, LegacyRulesMigration
from .cli_paths import (
    _is_lexically_canonical_absolute,
    _link_or_reparse_status,
    is_absolute_destination,
    is_local_folder_name,
    normalize_match_text,
    normalize_text,
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
