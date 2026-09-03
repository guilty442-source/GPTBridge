from __future__ import annotations

import os
import re
import stat as stat_module
from pathlib import Path, PurePosixPath, PureWindowsPath

from governance_rule.permission_directory.directory_authority import (
    GPTBRIDGE_PROJECT_ROOT,
    directory_authority_snapshot,
)


def permission_denied() -> PermissionError:
    return PermissionError("PERMISSION_DENIED")


def _canonical_project_root(project_root: Path) -> Path:
    try:
        requested = Path(project_root)
        configured = Path(GPTBRIDGE_PROJECT_ROOT)
    except (TypeError, ValueError, OSError) as exc:
        raise permission_denied() from exc
    if not requested.is_absolute() or not configured.is_absolute():
        raise permission_denied()
    try:
        requested_absolute = Path(os.path.abspath(requested))
        configured_absolute = Path(os.path.abspath(configured))
    except (TypeError, ValueError, OSError) as exc:
        raise permission_denied() from exc
    if os.path.normcase(str(requested_absolute)) != os.path.normcase(
        str(configured_absolute)
    ):
        raise permission_denied()
    try:
        root = requested_absolute.resolve()
        configured_root = configured_absolute.resolve()
    except (OSError, RuntimeError) as exc:
        raise permission_denied() from exc
    if root != configured_root:
        raise permission_denied()
    return root


def _stat_is_link_or_reparse(path_stat: os.stat_result) -> bool:
    attributes = int(getattr(path_stat, "st_file_attributes", 0) or 0)
    return stat_module.S_ISLNK(path_stat.st_mode) or bool(attributes & 0x400)


def _reject_path_aliases(project_root: Path, path_parts: tuple[str, ...]) -> None:
    current = project_root
    for part in path_parts:
        current = current / part
        try:
            path_stat = current.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise permission_denied() from exc
        if _stat_is_link_or_reparse(path_stat):
            raise permission_denied()
        if (
            stat_module.S_ISREG(path_stat.st_mode)
            and int(getattr(path_stat, "st_nlink", 1)) > 1
        ):
            raise permission_denied()


def _canonical_relative_path(relative_path: str) -> PurePosixPath:
    if not isinstance(relative_path, str) or not relative_path:
        raise permission_denied()
    path = PurePosixPath(relative_path)
    if relative_path == ".":
        return path
    if (
        path.is_absolute()
        or PureWindowsPath(relative_path).is_absolute()
        or not path.parts
        or ".." in path.parts
        or "\\" in relative_path
        or "\0" in relative_path
        or path.as_posix() != relative_path
        or any(":" in part for part in path.parts)
    ):
        raise permission_denied()
    return path


def resolve_project_path(project_root: Path, relative_path: str) -> Path:
    project_root = _canonical_project_root(project_root)
    path = _canonical_relative_path(relative_path)
    if relative_path == ".":
        return project_root
    _reject_path_aliases(project_root, path.parts)
    try:
        candidate = project_root.joinpath(*path.parts).resolve()
    except (OSError, RuntimeError) as exc:
        raise permission_denied() from exc
    try:
        candidate.relative_to(project_root)
    except ValueError as exc:
        raise permission_denied() from exc
    return candidate


def independent_tool_root(project_root: Path, tool_id: str) -> Path:
    authority = directory_authority_snapshot()
    if not isinstance(tool_id, str):
        raise permission_denied()
    normalized = tool_id.strip().lower()

    if not re.fullmatch(authority.tool_id_pattern, normalized):
        raise permission_denied()
    tools_root = _canonical_project_root(project_root)
    _reject_path_aliases(tools_root, (normalized,))
    candidate = (tools_root / normalized).resolve()
    try:
        relative = candidate.relative_to(tools_root)
    except ValueError as exc:
        raise permission_denied() from exc
    if len(relative.parts) != 1:
        raise permission_denied()
    return candidate


def tool_runtime_write_roots(
    project_root: Path,
    tool_id: str,
) -> tuple[Path, ...]:
    root = independent_tool_root(project_root, tool_id)
    values = (
        (root / "runtime" / "settings").resolve(),
        (root / "data" / "business").resolve(),
    )
    for value in values:
        try:
            value.relative_to(root)
        except ValueError as exc:
            raise permission_denied() from exc
    return values


def validate_grant_resource_path(
    project_root: Path,
    resource_path: str | None,
    grant: object,
    tool_id: str,
) -> None:
    if grant.path_match == "none":
        if resource_path is not None:
            raise permission_denied()
        return
    if resource_path is None:
        raise permission_denied()
    if resource_path.startswith("postgresql:") or resource_path.startswith("local:"):
        if grant.path_match != "exact" or resource_path not in {
            template.format(tool_id=tool_id) for template in grant.path_roots
        }:
            raise permission_denied()
        return
    resource = resolve_project_path(project_root, resource_path)
    allowed = False
    for template in grant.path_roots:
        root = resolve_project_path(
            project_root,
            template.format(tool_id=tool_id),
        )
        if grant.path_match == "exact":
            matches = resource == root
        elif grant.path_match == "within":
            try:
                resource.relative_to(root)
                matches = True
            except ValueError:
                matches = False
        else:
            raise permission_denied()
        if matches:
            allowed = True
            break
    if not allowed:
        raise permission_denied()
    for template in grant.excluded_path_roots:
        excluded = resolve_project_path(
            project_root,
            template.format(tool_id=tool_id),
        )
        try:
            resource.relative_to(excluded)
            raise permission_denied()
        except ValueError:
            continue
