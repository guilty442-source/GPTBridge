"""Shared helpers for the confirmation service (A366)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .auto_action_policy import compute_action_digest, read_pending_actions
from .sovereign_utils import _iso_now

CONFIRMATION_AUDIT_RELATIVE = (
    "main-system",
    "runtime",
    "state",
    "confirmation-audit.jsonl",
)


def _result(ok: bool, **fields: Any) -> dict[str, Any]:
    return {"ok": ok, **fields}


def _project_root(app: Any) -> Path:
    raw = getattr(app, "project_root", None)
    if raw:
        return Path(raw)
    # core_system/confirmation_service.py -> src-core -> main-system -> root
    return Path(__file__).resolve().parents[3]


def _audit(project_root: Path, entry: dict[str, Any]) -> None:
    """Append a confirmation audit line (A366 AUDIT, no secrets)."""
    try:
        path = project_root.joinpath(*CONFIRMATION_AUDIT_RELATIVE)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {"timestamp": _iso_now(), **entry},
                    ensure_ascii=False,
                    sort_keys=True,
                )
                + "\n"
            )
    except OSError:
        pass


def _find_action(project_root: Path, action_id: str) -> dict[str, Any] | None:
    return next(
        (
            item
            for item in read_pending_actions(project_root)
            if item.get("action_id") == action_id
        ),
        None,
    )


def _expired(action: dict[str, Any]) -> bool:
    raw = str(action.get("expires_at") or "").strip()
    if not raw:
        return False
    try:
        expiry = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return True
    return datetime.now(timezone.utc) > expiry


def _confirmation_of(action: dict[str, Any]) -> dict[str, Any]:
    value = action.get("confirmation")
    return value if isinstance(value, dict) else {}


def _confirmation_expired(confirmation: dict[str, Any]) -> bool:
    raw = str(confirmation.get("expires_at") or "").strip()
    if not raw:
        return False
    try:
        expiry = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return True
    return datetime.now(timezone.utc) > expiry


def _other_action_executing(project_root: Path, action_id: str) -> bool:
    return any(
        action.get("action_id") != action_id
        and action.get("status") == "executing"
        for action in read_pending_actions(project_root)
    )


def _refresh_remaining_evidence(project_root: Path, executed_id: str) -> None:
    """Recompute evidence digests for actions still awaiting confirmation."""
    actions = read_pending_actions(project_root)
    changed = False
    for action in actions:
        if action.get("action_id") == executed_id:
            continue
        if action.get("status") != "awaiting-confirmation":
            continue
        digest = compute_action_digest(action)
        if action.get("evidence_digest") != digest:
            action["evidence_digest"] = digest
            action["updated_at"] = _iso_now()
            changed = True
    if changed:
        from .auto_action_policy import _write_pending_actions

        _write_pending_actions(project_root, actions)
