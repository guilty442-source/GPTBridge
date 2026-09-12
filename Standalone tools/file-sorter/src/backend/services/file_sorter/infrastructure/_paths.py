"""Path validation, state root resolution, and profile identity utilities."""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from ._constants import (
    DEFAULT_EXCLUDE,
    DEFAULT_INCLUDE,
    SorterV2Error,
    _SAFE_ID_RE,
)


def resolve_state_root(explicit: str | Path | None = None) -> Path:
    """Return the tool-owned state root, without creating it."""

    from . import sorter_engine

    tool_root = sorter_engine.TOOL_ROOT
    configured = (
        str(explicit)
        if explicit is not None
        else os.environ.get("FILE_SORTER_STATE_ROOT", "").strip()
    )
    candidate = (
        Path(configured).expanduser().resolve()
        if configured
        else (tool_root / "runtime" / "state").resolve()
    )
    if not candidate.is_relative_to(tool_root):
        raise PermissionError("FILE_SORTER_STATE_SCOPE_DENIED")
    return candidate


def profile_id_for(target_dir: str | Path, profile: str | None = None) -> str:
    target = Path(target_dir).expanduser().resolve()
    normalized = os.path.normcase(str(target))
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
    if not profile:
        return f"target-{digest}"
    slug = re.sub(r"[^a-zA-Z0-9_.-]+", "-", profile.strip()).strip(".-")
    if not slug:
        raise SorterV2Error("Profile name must contain a letter or number.")
    name_digest = hashlib.sha256(profile.strip().encode("utf-8")).hexdigest()[:10]
    return f"{slug[:37]}-{name_digest}-{digest}"


def profile_path(
    target_dir: str | Path,
    *,
    state_root: str | Path | None = None,
    profile: str | None = None,
) -> Path:
    profile_id = profile_id_for(target_dir, profile)
    return resolve_state_root(state_root) / "profiles" / profile_id / "profile.json"


def _is_link_or_reparse(path: Path) -> bool:
    try:
        path_stat = path.lstat()
    except FileNotFoundError:
        return False
    except OSError as error:
        raise SorterV2Error(f"Cannot safely inspect path {path}: {error}") from error
    attributes = int(getattr(path_stat, "st_file_attributes", 0) or 0)
    return path.is_symlink() or bool(attributes & 0x400)


def _same_path_identity(left: Path, right: Path) -> bool:
    return os.path.normcase(str(left)) == os.path.normcase(str(right))


def _validated_target_directory(
    target_dir: str | Path,
    *,
    label: str = "Target directory",
) -> Path:
    requested = Path(target_dir).expanduser()
    if not requested.is_absolute():
        raise SorterV2Error(f"{label} must be absolute: {requested}")
    try:
        canonical = requested.resolve(strict=True)
    except OSError as error:
        raise SorterV2Error(f"{label} cannot be resolved: {requested}: {error}") from error
    if not _same_path_identity(requested, canonical):
        raise SorterV2Error(f"{label} must be canonical: {requested}")
    if _is_link_or_reparse(requested):
        raise SorterV2Error(f"{label} cannot be a link or reparse point: {requested}")
    if not canonical.is_dir():
        raise SorterV2Error(f"{label} does not exist: {canonical}")
    return canonical


def _state_category_root(
    state_root: str | Path | None,
    category: str,
) -> Path:
    root = resolve_state_root(state_root)
    existing_ancestor = root
    while (
        not existing_ancestor.exists()
        and not existing_ancestor.is_symlink()
        and existing_ancestor != existing_ancestor.parent
    ):
        existing_ancestor = existing_ancestor.parent
    if (
        _is_link_or_reparse(existing_ancestor)
        or not existing_ancestor.is_dir()
        or not _same_path_identity(
            existing_ancestor,
            existing_ancestor.resolve(strict=True),
        )
    ):
        raise SorterV2Error(f"Invalid state root boundary: {root}")
    if root.exists():
        if (
            _is_link_or_reparse(root)
            or not root.is_dir()
            or not _same_path_identity(root, root.resolve(strict=True))
        ):
            raise SorterV2Error(f"Invalid state root: {root}")
    category_root = root / category
    if category_root.exists():
        if _is_link_or_reparse(category_root) or not category_root.is_dir():
            raise SorterV2Error(
                f"State category cannot be a link or reparse point: {category_root}"
            )
        canonical = category_root.resolve(strict=True)
        if not _same_path_identity(category_root, canonical):
            raise SorterV2Error(f"State category escaped its root: {category_root}")
    return category_root


def _validated_state_document_path(
    path: Path,
    *,
    state_root: str | Path | None,
    category: str,
    relative_parts: int,
    require_exists: bool,
) -> Path:
    category_root = _state_category_root(state_root, category)
    requested = Path(path)
    if not requested.is_absolute():
        raise SorterV2Error(f"State document must be absolute: {requested}")
    try:
        relative = requested.relative_to(category_root)
    except ValueError as error:
        raise SorterV2Error(
            f"State document escaped the {category} directory: {requested}"
        ) from error
    if len(relative.parts) != relative_parts or any(
        part in {"", ".", ".."} for part in relative.parts
    ):
        raise SorterV2Error(f"Invalid {category} state path: {requested}")

    current = category_root
    for part in relative.parts[:-1]:
        current = current / part
        if current.exists() or current.is_symlink():
            if _is_link_or_reparse(current) or not current.is_dir():
                raise SorterV2Error(
                    f"State path cannot use a link or reparse point: {current}"
                )
            canonical_parent = current.resolve(strict=True)
            if not _same_path_identity(current, canonical_parent):
                raise SorterV2Error(f"State path escaped its root: {current}")

    exists_or_link = requested.exists() or requested.is_symlink()
    if require_exists and not exists_or_link:
        raise SorterV2Error(f"State document does not exist: {requested}")
    if exists_or_link:
        if _is_link_or_reparse(requested):
            raise SorterV2Error(
                f"State document cannot be a link or reparse point: {requested}"
            )
        try:
            canonical = requested.resolve(strict=True)
        except OSError as error:
            raise SorterV2Error(
                f"Cannot safely resolve state document {requested}: {error}"
            ) from error
        if not _same_path_identity(requested, canonical):
            raise SorterV2Error(f"State document escaped its root: {requested}")
        if not canonical.is_file():
            raise SorterV2Error(f"State document is not a regular file: {requested}")
    return requested


def _clean_patterns(
    values: Iterable[object] | None,
    *,
    default: Sequence[str],
) -> tuple[str, ...]:
    if values is None:
        return tuple(default)
    cleaned = tuple(str(item).strip() for item in values if str(item).strip())
    return cleaned


def _clean_rule_dicts(
    rules: Iterable[Mapping[str, Any]],
) -> tuple[dict[str, str], ...]:
    cleaned: list[dict[str, str]] = []
    for item in rules:
        keyword = str(item.get("keyword", "")).strip()
        folder = str(item.get("folder", "")).strip()
        if not keyword or not folder:
            continue
        folder_path = Path(folder).expanduser()
        if (
            folder_path.is_absolute()
            or folder in {".", ".."}
            or folder_path.name != folder
            or "/" in folder
            or "\\" in folder
        ):
            raise SorterV2Error(
                "Profile destination rules must name one direct child folder."
            )
        cleaned.append({"keyword": keyword, "folder": folder})
    return tuple(cleaned)


def _validated_id(value: str, label: str) -> str:
    cleaned = str(value).strip()
    if not _SAFE_ID_RE.fullmatch(cleaned):
        raise SorterV2Error(f"Invalid {label}: {value}")
    return cleaned


def _validate_operation_paths(
    target: Path,
    operation: PlanOperation,
    *,
    allow_missing_source: bool = False,
) -> tuple[Path, Path]:
    from ._models import PlanOperation  # noqa: F811 — type hint only

    target = _validated_target_directory(target)
    source = Path(operation.source)
    destination = Path(operation.destination)
    if not source.is_absolute() or source.parent != target:
        raise SorterV2Error(f"Source escaped plan target: {source}")
    if not destination.is_absolute():
        raise SorterV2Error(f"Destination is not absolute: {destination}")
    folder = Path(operation.folder.strip()).expanduser()
    if folder.is_absolute():
        raise SorterV2Error(
            f"Destination rule escaped plan target: {operation.folder}"
        )
    if (
        not operation.folder.strip()
        or operation.folder.strip() in {".", ".."}
        or folder.name != operation.folder.strip()
        or "/" in operation.folder
        or "\\" in operation.folder
    ):
        raise SorterV2Error(
            f"Invalid destination rule in plan: {operation.folder}"
        )
    expected_destination_dir = target / operation.folder.strip()
    if expected_destination_dir.parent != target:
        raise SorterV2Error(
            f"Destination escaped plan target: {expected_destination_dir}"
        )
    if (
        not expected_destination_dir.exists()
        or not expected_destination_dir.is_dir()
    ):
        raise SorterV2Error(
            f"Destination directory no longer exists: {expected_destination_dir}"
        )
    if _is_link_or_reparse(expected_destination_dir):
        raise SorterV2Error("Plan paths cannot use links or reparse points.")
    if not _same_path_identity(
        expected_destination_dir,
        expected_destination_dir.resolve(strict=True),
    ):
        raise SorterV2Error(
            f"Destination directory escaped plan target: {expected_destination_dir}"
        )
    if (
        os.path.ismount(expected_destination_dir)
        or expected_destination_dir.stat().st_dev != target.stat().st_dev
    ):
        raise SorterV2Error(
            "Destination directory cannot cross a mounted filesystem boundary."
        )
    if destination.parent != expected_destination_dir:
        raise SorterV2Error(
            f"Destination does not match the plan rule: {destination}"
        )
    source_exists_or_link = source.exists() or source.is_symlink()
    if not source_exists_or_link and not allow_missing_source:
        raise SorterV2Error(f"Source no longer exists: {source}")
    if source_exists_or_link:
        if _is_link_or_reparse(source):
            raise SorterV2Error("Plan paths cannot use links or reparse points.")
        canonical_source = source.resolve(strict=True)
        if (
            not _same_path_identity(source, canonical_source)
            or canonical_source.parent != target
            or not canonical_source.is_file()
            or canonical_source.stat().st_dev != target.stat().st_dev
        ):
            raise SorterV2Error(f"Source escaped plan target: {source}")
    if destination.exists() or destination.is_symlink():
        if _is_link_or_reparse(destination):
            raise SorterV2Error("Plan paths cannot use links or reparse points.")
        canonical_destination = destination.resolve(strict=True)
        if (
            not _same_path_identity(destination, canonical_destination)
            or canonical_destination.parent != expected_destination_dir
            or not canonical_destination.is_file()
        ):
            raise SorterV2Error(f"Destination escaped plan target: {destination}")
    return source, destination


def _validate_journal_operation_paths(
    journal: Mapping[str, Any],
    operation: Mapping[str, Any],
) -> tuple[Path, Path, Path | None]:
    from ._models import PlanOperation

    target_text = str(journal.get("target_dir", "")).strip()
    if not target_text:
        raise SorterV2Error("Journal does not contain a target directory.")
    target = _validated_target_directory(
        target_text,
        label="Journal target directory",
    )

    source = Path(str(operation.get("source", ""))).expanduser()
    destination = Path(str(operation.get("destination", ""))).expanduser()
    if not source.is_absolute() or source.parent != target:
        raise SorterV2Error("Journal source escaped its target directory.")
    if not destination.is_absolute():
        raise SorterV2Error("Journal destination is not absolute.")

    folder = str(operation.get("folder", "")).strip()
    if not folder:
        raise SorterV2Error(
            "Journal operation lacks a bounded destination rule."
        )
    probe = PlanOperation(
        operation_id=str(operation.get("operation_id") or "legacy-operation"),
        source=str(source),
        destination=str(destination),
        keyword=str(operation.get("keyword", "")),
        folder=folder,
        rule_source=str(operation.get("rule_source", "legacy")),
        source_size=int(operation.get("source_size") or 0),
        source_mtime_ns=int(operation.get("source_mtime_ns") or 0),
        transfer=str(operation.get("transfer", "unknown")),
    )
    source, destination = _validate_operation_paths(
        target,
        probe,
        allow_missing_source=True,
    )

    stage_value = operation.get("staging")
    if not stage_value:
        return source, destination, None
    stage = Path(str(stage_value)).expanduser()
    operation_id = str(operation.get("operation_id", "")).strip()
    transaction_id = str(journal.get("transaction_id", "")).strip()
    expected_stage_name = (
        f".filesorter-{transaction_id[:8]}-{operation_id[:8]}.partial"
    )
    if (
        not stage.is_absolute()
        or not operation_id
        or not transaction_id
        or stage.name != expected_stage_name
        or stage.parent != destination.parent
    ):
        raise SorterV2Error("Journal staging path is not owned by this operation.")
    if stage.exists() or stage.is_symlink():
        if _is_link_or_reparse(stage):
            raise SorterV2Error("Journal staging path cannot be a link.")
        if not _same_path_identity(stage, stage.resolve(strict=True)):
            raise SorterV2Error("Journal staging path escaped its directory.")
    return source, destination, stage
