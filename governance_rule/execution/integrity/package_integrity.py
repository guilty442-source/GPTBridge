from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path, PurePosixPath, PureWindowsPath
import stat as stat_module
from typing import Any, Iterable


PACKAGE_METADATA_NAME = ".gptbridge-package.json"
PACKAGE_FORMAT_VERSION = 1
SOURCE_IGNORED_DIRECTORY_NAMES = frozenset(
    {
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "browser-profile",
        "browser-profiles",
        "edge-profile",
        "runtime",
    }
)


def declared_source_exclusions(
    project_root: Path,
    tool_id: str,
) -> frozenset[str] | None:
    """Return the tool-owned package exclusions declared by its manifest."""

    if re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,63}", tool_id) is None:
        return None
    manifest_path = project_root / tool_id / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    package = manifest.get("package") if isinstance(manifest, dict) else None
    raw_exclusions = (
        package.get("source_excludes", [])
        if isinstance(package, dict)
        else []
    )
    normalized = _normalized_metadata_paths(raw_exclusions)
    if normalized is None:
        return None
    prefix = PurePosixPath(tool_id)
    return frozenset((prefix / item).as_posix() for item in normalized)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _stat_is_link_or_reparse(path_stat: os.stat_result) -> bool:
    attributes = int(getattr(path_stat, "st_file_attributes", 0) or 0)
    return stat_module.S_ISLNK(path_stat.st_mode) or bool(attributes & 0x400)


def snapshot_digest(files: dict[str, str]) -> str:
    digest = hashlib.sha256()
    for relative_path, file_digest in sorted(files.items()):
        digest.update(relative_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(file_digest.encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _normalized_metadata_paths(
    value: object,
    *,
    allow_dot: bool = False,
) -> tuple[str, ...] | None:
    if not isinstance(value, list) or not all(
        isinstance(item, str) for item in value
    ):
        return None
    normalized: list[str] = []
    for item in value:
        if (
            not item
            or "\\" in item
            or "\0" in item
            or PurePosixPath(item).is_absolute()
            or PureWindowsPath(item).is_absolute()
        ):
            return None
        candidate = PurePosixPath(item)
        canonical = candidate.as_posix()
        if (
            canonical != item
            or (canonical == "." and not allow_dot)
            or ".." in candidate.parts
            or any(":" in part for part in candidate.parts)
        ):
            return None
        normalized.append(canonical)
    if len(normalized) != len(set(normalized)):
        return None
    return tuple(normalized)


def _path_is_below_root(relative_path: str, relative_root: str) -> bool:
    path_parts = PurePosixPath(relative_path).parts
    root_parts = PurePosixPath(relative_root).parts
    if relative_root == ".":
        root_parts = ()
    return (
        len(path_parts) > len(root_parts)
        and path_parts[: len(root_parts)] == root_parts
    )


def _path_is_at_or_below_root(relative_path: str, relative_root: str) -> bool:
    return (
        relative_path == relative_root
        or _path_is_below_root(relative_path, relative_root)
    )


def collect_file_hashes(
    base_dir: Path,
    relative_roots: Iterable[str],
    *,
    ignored_directory_names: frozenset[str] = frozenset(),
    excluded_relative_paths: frozenset[str] = frozenset(),
) -> dict[str, str]:
    base = base_dir.resolve()
    files: dict[str, str] = {}

    def add_file(candidate: Path) -> None:
        try:
            lexical_relative = candidate.relative_to(base).as_posix()
        except ValueError as error:
            raise ValueError(
                f"Package input escaped its base directory: {candidate}"
            ) from error
        if lexical_relative in excluded_relative_paths:
            return
        candidate_stat = candidate.lstat()
        if _stat_is_link_or_reparse(candidate_stat):
            raise ValueError(
                f"Package input cannot be a link or reparse point: {candidate}"
            )
        if not stat_module.S_ISREG(candidate_stat.st_mode):
            raise ValueError(f"Package input is not a regular file: {candidate}")
        resolved = candidate.resolve()
        try:
            relative = resolved.relative_to(base).as_posix()
        except ValueError as error:
            raise ValueError(f"Package input escaped its base directory: {candidate}") from error
        if candidate.suffix.lower() in {".pyc", ".pyo"}:
            return
        files[relative] = _sha256_file(candidate)

    for raw_root in relative_roots:
        requested_root = Path(os.path.abspath(base / raw_root))
        try:
            requested_root.relative_to(base)
        except ValueError as error:
            raise ValueError(f"Package input root escaped the project: {raw_root}") from error
        try:
            root_stat = requested_root.lstat()
        except FileNotFoundError as error:
            raise FileNotFoundError(
                f"Package input not found: {requested_root}"
            ) from error
        if _stat_is_link_or_reparse(root_stat):
            raise ValueError(
                "Package input root cannot be a link or reparse point: "
                f"{requested_root}"
            )
        root = requested_root.resolve(strict=True)
        try:
            root.relative_to(base)
        except ValueError as error:
            raise ValueError(
                f"Package input root escaped the project: {raw_root}"
            ) from error
        if stat_module.S_ISREG(root_stat.st_mode):
            add_file(root)
            continue
        if not stat_module.S_ISDIR(root_stat.st_mode):
            raise ValueError(f"Package input root is not a file or directory: {root}")

        for current_root, directory_names, file_names in os.walk(root):
            current_path = Path(current_root)
            current_stat = current_path.lstat()
            if _stat_is_link_or_reparse(current_stat):
                raise ValueError(
                    "Package input cannot contain a link or reparse point: "
                    f"{current_path}"
                )
            retained_directories: list[str] = []
            for directory_name in sorted(directory_names):
                directory = current_path / directory_name
                directory_relative = directory.relative_to(base).as_posix()
                if directory_relative in excluded_relative_paths:
                    raise ValueError(
                        "Excluded package input path must not be a directory: "
                        f"{directory}"
                    )
                directory_stat = directory.lstat()
                if _stat_is_link_or_reparse(directory_stat):
                    raise ValueError(
                        "Package input cannot contain a link or reparse point: "
                        f"{directory}"
                    )
                if directory_name not in ignored_directory_names:
                    retained_directories.append(directory_name)
            directory_names[:] = retained_directories
            for file_name in sorted(file_names):
                add_file(current_path / file_name)

    return dict(sorted(files.items()))


def load_package_metadata(app_dir: Path) -> dict[str, Any]:
    metadata_path = app_dir / PACKAGE_METADATA_NAME
    try:
        data = json.loads(metadata_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError):
        return {"_invalid": True}
    return data if isinstance(data, dict) else {"_invalid": True}


def verify_packaged_app(
    app_dir: Path,
    *,
    project_root: Path | None = None,
) -> dict[str, Any]:
    metadata = load_package_metadata(app_dir)
    if not metadata:
        return {
            "ok": False,
            "error_code": "PACKAGE_UNVERIFIED",
            "message": f"Missing {PACKAGE_METADATA_NAME}",
        }
    if metadata.get("_invalid") or metadata.get("format_version") != PACKAGE_FORMAT_VERSION:
        return {
            "ok": False,
            "error_code": "PACKAGE_UNVERIFIED",
            "message": "Invalid or unsupported package metadata",
        }

    expected_payload = metadata.get("payload_files")
    if not isinstance(expected_payload, dict) or not all(
        isinstance(key, str) and isinstance(value, str)
        for key, value in expected_payload.items()
    ):
        return {
            "ok": False,
            "error_code": "PACKAGE_UNVERIFIED",
            "message": "Package metadata has no valid payload snapshot",
        }

    try:
        current_payload = collect_file_hashes(
            app_dir,
            ["."],
            excluded_relative_paths=frozenset({PACKAGE_METADATA_NAME}),
        )
    except (OSError, ValueError) as error:
        return {
            "ok": False,
            "error_code": "PACKAGE_CORRUPT",
            "message": f"Could not verify packaged payload: {error}",
        }
    expected_payload_digest = str(metadata.get("payload_digest", ""))
    current_payload_digest = snapshot_digest(current_payload)
    if current_payload != expected_payload or current_payload_digest != expected_payload_digest:
        return {
            "ok": False,
            "error_code": "PACKAGE_CORRUPT",
            "message": "Packaged files do not match their content fingerprint",
            "expected_digest": expected_payload_digest,
            "actual_digest": current_payload_digest,
        }

    source_check = "unavailable"
    if project_root is not None:
        raw_source_roots = metadata.get("source_roots")
        expected_source = metadata.get("source_files")
        if raw_source_roots is not None or expected_source is not None:
            source_roots = _normalized_metadata_paths(
                raw_source_roots,
                allow_dot=True,
            )
            source_excluded_paths = _normalized_metadata_paths(
                metadata.get("source_excluded_paths", []),
            )
            expected_source_valid = (
                isinstance(expected_source, dict)
                and all(
                    isinstance(key, str) and isinstance(value, str)
                    for key, value in expected_source.items()
                )
            )
            expected_source_paths = (
                _normalized_metadata_paths(list(expected_source))
                if expected_source_valid
                else None
            )
            tool_id = str(metadata.get("tool_id", ""))
            declared_exclusions = declared_source_exclusions(
                project_root,
                tool_id,
            )
            exclusion_scope_valid = (
                source_excluded_paths is not None
                and (
                    (
                        declared_exclusions is None
                        and not source_excluded_paths
                    )
                    or frozenset(source_excluded_paths)
                    == declared_exclusions
                )
            )
            if (
                source_roots is None
                or not source_roots
                or source_excluded_paths is None
                or expected_source_paths is None
                or not exclusion_scope_valid
                or any(
                    not any(
                        _path_is_below_root(excluded_path, source_root)
                        for source_root in source_roots
                    )
                    for excluded_path in source_excluded_paths
                )
                or any(
                    not any(
                        _path_is_at_or_below_root(
                            expected_path,
                            source_root,
                        )
                        for source_root in source_roots
                    )
                    for expected_path in expected_source_paths
                )
                or any(
                    excluded_path in expected_source
                    for excluded_path in source_excluded_paths
                )
            ):
                return {
                    "ok": False,
                    "error_code": "PACKAGE_UNVERIFIED",
                    "message": "Package metadata has an invalid source snapshot",
                }
            tool_source_root = tool_id
            effective_source_roots = [
                item
                for item in source_roots
                if _path_is_at_or_below_root(item, tool_source_root)
                or item == "main-system/config/tool-runtime-contract.json"
                or _path_is_at_or_below_root(
                    item,
                    "shared-layer/src/shared_layer",
                )
            ]
            legacy_infrastructure_snapshot = (
                tuple(effective_source_roots) != tuple(source_roots)
            )
            effective_expected_source = (
                {
                    path: digest
                    for path, digest in expected_source.items()
                    if any(
                        _path_is_at_or_below_root(path, source_root)
                        for source_root in effective_source_roots
                    )
                }
                if legacy_infrastructure_snapshot
                else expected_source
            )
            roots_available = bool(effective_source_roots) and all(
                (project_root / item).exists()
                for item in effective_source_roots
            )
            if roots_available:
                try:
                    current_source = collect_file_hashes(
                        project_root,
                        effective_source_roots,
                        ignored_directory_names=SOURCE_IGNORED_DIRECTORY_NAMES,
                        excluded_relative_paths=frozenset(
                            source_excluded_paths
                        ),
                    )
                except (OSError, ValueError) as error:
                    return {
                        "ok": False,
                        "error_code": "STALE_PACKAGE",
                        "message": f"Could not verify package inputs: {error}",
                    }
                current_source_digest = snapshot_digest(current_source)
                expected_source_digest = (
                    snapshot_digest(effective_expected_source)
                    if legacy_infrastructure_snapshot
                    else str(metadata.get("source_digest", ""))
                )
                if (
                    current_source != effective_expected_source
                    or current_source_digest != expected_source_digest
                ):
                    return {
                        "ok": False,
                        "error_code": "STALE_PACKAGE",
                        "message": "Source files changed after this EXE was packaged",
                        "expected_digest": expected_source_digest,
                        "actual_digest": current_source_digest,
                    }
                source_check = (
                    "current-runtime-contract"
                    if legacy_infrastructure_snapshot
                    else "current"
                )

    return {
        "ok": True,
        "tool_id": str(metadata.get("tool_id", "")),
        "package_digest": current_payload_digest,
        "source_check": source_check,
    }
