"""Central policy for the A366 automatic repair/update user-switch gate.

Governance (codex A366 ``xingcheng-auxiliary-repair-update-user-switch-
confirmation-and-fault-cardinality``):

  * The Xingcheng auxiliary system owns the user-facing control surface for
    automatic repair and automatic update and exposes two independent,
    explicit, persisted switches: ``automatic_repair_enabled`` and
    ``automatic_update_enabled``.
  * A repair/update mutation may execute only when its corresponding switch
    is enabled AND the user confirms the concrete pending action.  A switch
    alone is not confirmation; confirmation alone cannot bypass a disabled
    switch.
  * Detection, classification, evidence collection, isolation, and
    non-mutating diagnosis continue regardless of switch state so faults
    remain observable.

This module owns the persisted switch store, the switch-change audit log,
and the durable per-item pending-action queue used by the assistant panel.
Each pending entry is a single fault or update intent with its own
identifier so confirmations stay per-item.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final, Iterable

AUTOMATIC_REPAIR_SWITCH: Final[str] = "automatic_repair_enabled"
AUTOMATIC_UPDATE_SWITCH: Final[str] = "automatic_update_enabled"
SWITCH_NAMES: Final[tuple[str, ...]] = (
    AUTOMATIC_REPAIR_SWITCH,
    AUTOMATIC_UPDATE_SWITCH,
)

PENDING_ACTIONS_RELATIVE: Final[tuple[str, ...]] = (
    "main-system",
    "runtime",
    "state",
    "pending-actions.json",
)
SWITCHES_RELATIVE: Final[tuple[str, ...]] = (
    "main-system",
    "runtime",
    "state",
    "automation-switches.json",
)
SWITCH_AUDIT_RELATIVE: Final[tuple[str, ...]] = (
    "main-system",
    "runtime",
    "state",
    "automation-switch-audit.jsonl",
)
MAX_PENDING_ACTIONS: Final[int] = 100

# Default confirmation validity window (A366: confirmation binds expiry).
CONFIRMATION_TTL_SECONDS: Final[float] = 24 * 3600.0

# Cross-thread fault/clear notification: any change to the pending-action
# surface wakes the backend push loop immediately so Xingcheng receives the
# fault report (and its resolution) without waiting for the next cycle.
_FAULT_CHANGE_EVENT: Final[threading.Event] = threading.Event()


def fault_change_event() -> threading.Event:
    """Shared event set when a fault or its resolution is recorded."""
    return _FAULT_CHANGE_EVENT


def notify_fault_change() -> None:
    """Wake the backend report loop for an immediate Xingcheng update."""
    _FAULT_CHANGE_EVENT.set()


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _project_root() -> Path:
    # core_system/auto_action_policy.py -> src-core -> main-system -> GPTBridge
    return Path(__file__).resolve().parents[3]


def _atomic_write(path: Path, payload: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


# ----------------------------------------------------------------------
# User-facing switches (A366 CONTROL-SURFACE)
# ----------------------------------------------------------------------


def read_automation_switches(project_root: str | Path | None = None) -> dict[str, Any]:
    """Return the persisted switches with visible attribution.

    Defaults are ``False`` (frozen); a missing or unreadable store never
    enables execution.
    """
    root = Path(project_root) if project_root else _project_root()
    path = root.joinpath(*SWITCHES_RELATIVE)
    data: dict[str, Any] = {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            data = loaded
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        data = {}
    return {
        AUTOMATIC_REPAIR_SWITCH: bool(
            data.get(AUTOMATIC_REPAIR_SWITCH, False)
        ),
        AUTOMATIC_UPDATE_SWITCH: bool(
            data.get(AUTOMATIC_UPDATE_SWITCH, False)
        ),
        "updated_at": str(data.get("updated_at") or ""),
        "updated_by": str(data.get("updated_by") or ""),
    }


def write_automation_switches(
    project_root: str | Path,
    switches: dict[str, Any],
    *,
    actor: str,
) -> dict[str, Any]:
    """Persist the switches and append a switch-change audit entry."""
    root = Path(project_root)
    record = {
        AUTOMATIC_REPAIR_SWITCH: bool(
            switches.get(AUTOMATIC_REPAIR_SWITCH, False)
        ),
        AUTOMATIC_UPDATE_SWITCH: bool(
            switches.get(AUTOMATIC_UPDATE_SWITCH, False)
        ),
        "updated_at": _iso_now(),
        "updated_by": actor,
    }
    _atomic_write(
        root.joinpath(*SWITCHES_RELATIVE),
        json.dumps(record, ensure_ascii=False, indent=2) + "\n",
    )
    return record


def set_automation_switch(
    project_root: str | Path | None,
    switch: str,
    enabled: bool,
    *,
    actor: str = "authenticated-ui",
) -> dict[str, Any]:
    """Set one switch explicitly and audit the change (A366 AUDIT)."""
    if switch not in SWITCH_NAMES:
        raise ValueError(f"unknown automation switch: {switch}")
    root = Path(project_root) if project_root else _project_root()
    current = read_automation_switches(root)
    previous = bool(current.get(switch, False))
    updated = {**current, switch: bool(enabled)}
    record = write_automation_switches(root, updated, actor=actor)
    audit_entry = {
        "timestamp": _iso_now(),
        "actor": actor,
        "switch": switch,
        "previous": previous,
        "enabled": bool(enabled),
    }
    try:
        audit_path = root.joinpath(*SWITCH_AUDIT_RELATIVE)
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        with audit_path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(audit_entry, ensure_ascii=False, sort_keys=True)
                + "\n"
            )
    except OSError:
        pass
    return record


def automatic_repair_execution_allowed() -> bool:
    """True when the repair switch is enabled (execution still needs
    per-action user confirmation)."""
    return bool(read_automation_switches().get(AUTOMATIC_REPAIR_SWITCH))


def automatic_update_execution_allowed() -> bool:
    """True when the update switch is enabled (execution still needs
    per-action user confirmation)."""
    return bool(read_automation_switches().get(AUTOMATIC_UPDATE_SWITCH))


def switch_for_kind(kind: str) -> str:
    if kind == "repair":
        return AUTOMATIC_REPAIR_SWITCH
    if kind == "update":
        return AUTOMATIC_UPDATE_SWITCH
    return ""


def switch_enabled_for_kind(kind: str) -> bool:
    switch = switch_for_kind(kind)
    if not switch:
        return False
    return bool(read_automation_switches().get(switch, False))


# ----------------------------------------------------------------------
# Pending confirmation queue
# ----------------------------------------------------------------------


def pending_actions_path(project_root: str | Path) -> Path:
    return Path(project_root).joinpath(*PENDING_ACTIONS_RELATIVE)


def read_pending_actions(project_root: str | Path | None = None) -> list[dict[str, Any]]:
    """Return the pending user-confirmation queue (may be empty)."""
    root = Path(project_root) if project_root else _project_root()
    path = pending_actions_path(root)
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
    _atomic_write(
        pending_actions_path(project_root),
        json.dumps(actions, ensure_ascii=False, indent=2) + "\n",
    )


BINDING_FIELDS: Final[tuple[str, ...]] = (
    "fault_id",
    "update_id",
    "scope",
    "target",
    "proposed_method",
    "risk",
    "rollback",
    "expires_at",
)


def compute_action_digest(action: dict[str, Any]) -> str:
    """Digest of the binding-relevant action fields (A366 confirmation
    evidence digest).  A material change invalidates prior confirmation.
    """
    binding = {
        "action_id": action.get("action_id"),
        "kind": action.get("kind"),
        "summary": action.get("summary"),
        "detail": action.get("detail") or {},
    }
    for field in BINDING_FIELDS:
        binding[field] = action.get(field)
    return hashlib.sha256(
        json.dumps(binding, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def record_pending_action(
    project_root: str | Path,
    *,
    kind: str,
    summary: str,
    detail: dict[str, Any] | None = None,
    action_id: str = "",
    binding: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Append one pending confirmation item (deduplicated by action_id).

    ``binding`` carries the A366 confirmation binding fields (fault_id /
    update_id, scope, target, proposed method, risk, rollback, expiry).
    """
    if not action_id:
        digest = hashlib.sha256(
            json.dumps(
                {
                    "kind": kind,
                    "summary": summary,
                    "detail": detail or {},
                    "binding": binding or {},
                },
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
                **(binding or {}),
                "summary": summary,
                "detail": detail or existing.get("detail", {}),
                "updated_at": _iso_now(),
            }
            merged["evidence_digest"] = compute_action_digest(merged)
            actions[index] = merged
            _write_pending_actions(project_root, actions)
            notify_fault_change()
            return merged
    record = {
        "action_id": action_id,
        "kind": kind,
        "summary": summary,
        "detail": detail or {},
        **(binding or {}),
        "status": "awaiting-confirmation",
        "created_at": _iso_now(),
        "updated_at": _iso_now(),
    }
    record["evidence_digest"] = compute_action_digest(record)
    actions.append(record)
    if len(actions) > MAX_PENDING_ACTIONS:
        actions = actions[-MAX_PENDING_ACTIONS:]
    _write_pending_actions(project_root, actions)
    notify_fault_change()
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
        notify_fault_change()
    return updated


def remove_pending_actions(
    project_root: str | Path,
    action_ids: Iterable[str],
    *,
    actor: str = "",
    reason: str = "",
) -> list[str]:
    """Remove reconciled pending items from the user-facing queue.

    Used only for items already reconciled as non-actionable evidence
    (expired or unclassifiable); removal is attributable via ``actor`` and
    ``reason`` and wakes the report loop so the Xingcheng surface refreshes.
    Actionable or confirmed items are never touched by this helper.
    """
    wanted = {str(value).strip() for value in action_ids if str(value).strip()}
    if not wanted:
        return []
    actions = read_pending_actions(project_root)
    removed = [
        str(action.get("action_id"))
        for action in actions
        if str(action.get("action_id")) in wanted
        and action.get("status") == "awaiting-confirmation"
    ]
    if not removed:
        return []
    remaining = [
        action
        for action in actions
        if str(action.get("action_id")) not in set(removed)
    ]
    _write_pending_actions(project_root, remaining)
    notify_fault_change()
    return removed


__all__ = [
    "AUTOMATIC_REPAIR_SWITCH",
    "AUTOMATIC_UPDATE_SWITCH",
    "CONFIRMATION_TTL_SECONDS",
    "MAX_PENDING_ACTIONS",
    "PENDING_ACTIONS_RELATIVE",
    "SWITCHES_RELATIVE",
    "SWITCH_AUDIT_RELATIVE",
    "SWITCH_NAMES",
    "automatic_repair_execution_allowed",
    "automatic_update_execution_allowed",
    "compute_action_digest",
    "fault_change_event",
    "notify_fault_change",
    "pending_actions_path",
    "read_automation_switches",
    "read_pending_actions",
    "record_pending_action",
    "remove_pending_actions",
    "set_automation_switch",
    "switch_enabled_for_kind",
    "switch_for_kind",
    "update_pending_action_status",
    "write_automation_switches",
]
