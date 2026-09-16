"""SQLite access boundaries.

SQLite has no roles, so the boundary is layered instead:

  * file ACL expectations (owner-only write, no world access),
  * a path allowlist rooted at the declared writable roots,
  * locator scope (module-scoped locators only), and
  * a per-module process identity (one SQLite connection identity per tool).

:func:`assess_access` turns those inputs into a fail-closed decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Final

WRITABLE_ROOT_PREFIXES: Final[tuple[str, ...]] = (
    "main-system/runtime/state",
    "shared-layer/data",
    "shared-layer/runtime",
)

MODULE_DATA_TEMPLATE: Final[str] = "{tool_id}/data/business"
MODULE_STATE_TEMPLATE: Final[str] = "{tool_id}/runtime/state"
MODULE_SETTINGS_TEMPLATE: Final[str] = "{tool_id}/runtime/settings"


class SqliteScopeError(RuntimeError):
    """Raised when a SQLite access request violates the boundary policy."""


@dataclass(frozen=True)
class SqliteAccessRequest:
    module_id: str
    path: str
    write: bool
    locator_module_id: str = ""
    process_identity: str = ""
    acl_owner_only: bool = True


def writable_roots(module_id: str) -> tuple[str, ...]:
    return (
        *WRITABLE_ROOT_PREFIXES,
        MODULE_DATA_TEMPLATE.format(tool_id=module_id),
        MODULE_STATE_TEMPLATE.format(tool_id=module_id),
        MODULE_SETTINGS_TEMPLATE.format(tool_id=module_id),
    )


def assess_access(request: SqliteAccessRequest, workspace_root: str | Path) -> None:
    """Fail closed unless every SQLite boundary layer agrees."""
    root = Path(workspace_root).resolve()
    candidate = (root / request.path).resolve() if not Path(request.path).is_absolute() else Path(request.path).resolve()
    try:
        relative = candidate.relative_to(root).as_posix()
    except ValueError as error:
        raise SqliteScopeError(f"SQLITE_PATH_OUTSIDE_WORKSPACE:{request.path}") from error

    if not request.write:
        if not request.module_id:
            raise SqliteScopeError("SQLITE_MODULE_IDENTITY_REQUIRED")
        return

    if not request.module_id:
        raise SqliteScopeError("SQLITE_MODULE_IDENTITY_REQUIRED")
    if not request.process_identity:
        raise SqliteScopeError("SQLITE_PROCESS_IDENTITY_REQUIRED")
    if request.process_identity != request.module_id:
        raise SqliteScopeError(
            f"SQLITE_PROCESS_IDENTITY_MISMATCH:{request.process_identity}:{request.module_id}"
        )
    if request.locator_module_id and request.locator_module_id != request.module_id:
        raise SqliteScopeError(
            f"SQLITE_LOCATOR_SCOPE_MISMATCH:{request.locator_module_id}:{request.module_id}"
        )
    if not request.acl_owner_only:
        raise SqliteScopeError("SQLITE_FILE_ACL_NOT_OWNER_ONLY")

    roots = writable_roots(request.module_id)
    global_roots_allowed = request.module_id in {"main-system", "shared-layer"}
    allowed = (
        (
            any(
                relative == root_path or relative.startswith(root_path + "/")
                for root_path in WRITABLE_ROOT_PREFIXES
            )
            and global_roots_allowed
        )
        or any(
            relative == root_path or relative.startswith(root_path + "/")
            for root_path in roots[len(WRITABLE_ROOT_PREFIXES):]
        )
        or relative.startswith(f"Standalone tools/{request.module_id}/")
    )
    if not allowed:
        raise SqliteScopeError(f"SQLITE_PATH_NOT_ALLOWLISTED:{relative}")


def expected_acl_commands(path: str | Path) -> list[str]:
    """The icacls commands the installer must apply for an owner-only file."""
    target = str(Path(path))
    return [
        f'icacls "{target}" /inheritance:r',
        f'icacls "{target}" /grant:r "%USERNAME%":(R,W)',
        f'icacls "{target}" /remove "Everyone" "Users" "Authenticated Users"',
    ]


__all__ = [
    "MODULE_DATA_TEMPLATE",
    "MODULE_SETTINGS_TEMPLATE",
    "MODULE_STATE_TEMPLATE",
    "SqliteAccessRequest",
    "SqliteScopeError",
    "WRITABLE_ROOT_PREFIXES",
    "assess_access",
    "expected_acl_commands",
    "writable_roots",
]
