"""Central policy for automatic repair/update execution.

Governance rule (user directive): a detected fault or an available update
must NOT execute automatically.  It is classified, recorded, and surfaced
in the assistant (Xingcheng) panel where the user confirms each item
individually.  Only explicit user confirmation releases execution.

The switches live in ``config/feature_flags.json`` and are hot-reloadable:

  * ``automatic_repair_execution`` — when false, repair decision/execution
    paths defer every request to user confirmation.
  * ``automatic_update_execution`` — when false, hot-reload / source-change
    update paths defer every update intent to user confirmation.

This module also owns the durable pending-action queue used by the
assistant panel.  Each entry is a single fault or update intent with its
own identifier so confirmations stay per-item.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

from startup_core.feature_flags import is_enabled

AUTOMATIC_REPAIR_FLAG: Final[str] = "automatic_repair_execution"
AUTOMATIC_UPDATE_FLAG: Final[str] = "automatic_update_execution"
USER_CONFIRMATION_RELEASE_FLAG: Final[str] = "user_confirmation_release"

PENDING_ACTIONS_RELATIVE: Final[tuple[str, ...]] = (
    "main-system",
    "runtime",
    "state",
    "pending-actions.json",
)
MAX_PENDING_ACTIONS: Final[int] = 100


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def automatic_repair_execution_allowed() -> bool:
    """True when repairs may execute without explicit user confirmation."""
    return is_enabled(AUTOMATIC_REPAIR_FLAG)


def automatic_update_execution_allowed() -> bool:
    """True when updates may execute without explicit user confirmation."""
    return is_enabled(AUTOMATIC_UPDATE_FLAG)


def user_confirmation_release_allowed() -> bool:
    """True when a user-confirmed pending action may execute.

    This is the operator's release switch.  While it is false the assistant
    panel is fully wired but every confirmation is refused, so no repair or
    update can execute even with a button press.
    """
    return is_enabled(USER_CONFIRMATION_RELEASE_FLAG)


def pending_actions_path(project_root: str | Path) -> Path:
    return Path(project_root).joinpath(*PENDING_ACTIONS_RELATIVE)


def read_pending_actions(project_root: str | Path) -> list[dict[str, Any]]:
    """Return the pending user-confirmation queue (may be empty)."""
    path = pending_actions_path(project_root)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return []
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def _write_pending_actions(
    project_root: str | Path, actions: list[dict[str, Any]]
) -> None:
    path = pending_actions_path(project_root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(actions, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, path)
    except OSError:
        pass


def record_pending_action(
    project_root: str | Path,
    *,
    kind: str,
    summary: str,
    detail: dict[str, Any] | None = None,
    action_id: str = "",
) -> dict[str, Any]:
    """Append one pending confirmation item (deduplicated by action_id).

    ``kind`` is ``"repair"`` or ``"update"``.  Returns the stored record.
    """
    if not action_id:
        digest = hashlib.sha256(
            json.dumps(
                {"kind": kind, "summary": summary, "detail": detail or {}},
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()[:16]
        action_id = f"{kind}-{digest}"
    actions = read_pending_actions(project_root)
    for index, existing in enumerate(actions):
        if existing.get("action_id") == action_id:
            merged = {
                **existing,
                "summary": summary,
                "detail": detail or existing.get("detail", {}),
                "updated_at": _iso_now(),
            }
            actions[index] = merged
            _write_pending_actions(project_root, actions)
            return merged
    record = {
        "action_id": action_id,
        "kind": kind,
        "summary": summary,
        "detail": detail or {},
        "status": "awaiting-confirmation",
        "created_at": _iso_now(),
        "updated_at": _iso_now(),
    }
    actions.append(record)
    if len(actions) > MAX_PENDING_ACTIONS:
        actions = actions[-MAX_PENDING_ACTIONS:]
    _write_pending_actions(project_root, actions)
    return record


def update_pending_action_status(
    project_root: str | Path,
    action_id: str,
    status: str,
    **fields: Any,
) -> dict[str, Any] | None:
    """Update one pending action's status and extra fields."""
    actions = read_pending_actions(project_root)
    updated: dict[str, Any] | None = None
    for index, existing in enumerate(actions):
        if existing.get("action_id") == action_id:
            actions[index] = {
                **existing,
                **fields,
                "status": status,
                "updated_at": _iso_now(),
            }
            updated = actions[index]
            break
    if updated is not None:
        _write_pending_actions(project_root, actions)
    return updated


__all__ = [
    "AUTOMATIC_REPAIR_FLAG",
    "AUTOMATIC_UPDATE_FLAG",
    "MAX_PENDING_ACTIONS",
    "PENDING_ACTIONS_RELATIVE",
    "USER_CONFIRMATION_RELEASE_FLAG",
    "automatic_repair_execution_allowed",
    "automatic_update_execution_allowed",
    "pending_actions_path",
    "read_pending_actions",
    "record_pending_action",
    "update_pending_action_status",
    "user_confirmation_release_allowed",
]
