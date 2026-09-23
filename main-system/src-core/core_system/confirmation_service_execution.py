"""Confirmation service — execution bodies.

Extracted from confirmation_service.py: the _execute_repair and
_execute_update functions that run the approved action through the
governance repair/update chain.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

from .sovereign_utils import _iso_now


def _result(ok: bool, **fields: Any) -> dict[str, Any]:
    return {"ok": ok, **fields}


async def _execute_repair(
    app: Any, project_root: Path, action: dict[str, Any]
) -> dict[str, Any]:
    from tasks.repair_coordinator import get_repair_coordinator

    action_id = str(action.get("action_id") or "")
    detail = action.get("detail") or {}
    request_id = str(action.get("fault_id") or detail.get("request_id") or "")
    coordinator = get_repair_coordinator()
    if coordinator is None or not request_id:
        return _result(
            False,
            error_code="REPAIR_REQUEST_UNAVAILABLE",
            message="repair coordinator or request id is unavailable",
            action_id=action_id,
        )
    request = coordinator.get_request(request_id)
    if request is None or request.get("status") != "awaiting-confirmation":
        return _result(
            False,
            error_code="REPAIR_REQUEST_NOT_PENDING",
            message="repair request is missing or not awaiting confirmation",
            action_id=action_id,
        )

    maintenance = getattr(app, "maintenance_sovereign", None)
    decision_sovereign = getattr(app, "decision_sovereign", None)
    if maintenance is None or decision_sovereign is None:
        return _result(
            False,
            error_code="REPAIR_CHAIN_UNAVAILABLE",
            message="maintenance or decision sovereign is unavailable",
            action_id=action_id,
        )

    classified = request.get("classified") or maintenance._classify_health_signal(
        request.get("decision_proof") or {}
    )
    coordinator.mark_request_status(
        request_id, "executing", executing_at=_iso_now()
    )
    return _route_repair_decision(
        coordinator, request_id, decision_sovereign, classified, action_id
    )


def _route_repair_decision(
    coordinator: Any,
    request_id: str,
    decision_sovereign: Any,
    classified: Any,
    action_id: str,
) -> dict[str, Any]:
    try:
        result = decision_sovereign.decide_and_route_repair(
            classified, user_confirmed=True
        )
    except Exception as error:
        detail_text = f"{type(error).__name__}: {error}"
        coordinator.mark_request_status(request_id, "failed", error=detail_text)
        return _result(
            False,
            error_code="REPAIR_EXECUTION_FAILED",
            message=detail_text,
            action_id=action_id,
        )

    ok = bool(result.get("ok"))
    decision = str(result.get("decision") or ("completed" if ok else "failed"))
    coordinator.acknowledge_request(request_id, decision=decision, ok=ok)
    return _result(
        ok,
        action_id=action_id,
        fault_id=request_id,
        decision=decision,
        result=result,
    )


async def _execute_update(
    app: Any, project_root: Path, action: dict[str, Any]
) -> dict[str, Any]:
    action_id = str(action.get("action_id") or "")
    detail = action.get("detail") or {}
    watcher = getattr(app, "hot_reload_watcher", None)
    if watcher is None:
        return _result(
            False,
            error_code="HOT_RELOAD_WATCHER_UNAVAILABLE",
            message="hot reload watcher is unavailable",
            action_id=action_id,
        )

    changed_paths = [
        str(path) for path in (detail.get("changed_paths") or []) if path
    ]
    if not changed_paths:
        for module_name in detail.get("modules") or []:
            module = sys.modules.get(str(module_name))
            file_path = getattr(module, "__file__", None)
            if file_path:
                changed_paths.append(str(file_path))
    if not changed_paths:
        return _result(
            False,
            error_code="UPDATE_TARGETS_UNAVAILABLE",
            message="pending update has no resolvable changed paths",
            action_id=action_id,
        )

    try:
        accepted = await watcher._maybe_reload(
            changed_paths, user_confirmed=True
        )
    except Exception as error:
        return _result(
            False,
            error_code="UPDATE_EXECUTION_FAILED",
            message=f"{type(error).__name__}: {error}",
            action_id=action_id,
        )

    return _result(
        bool(accepted),
        action_id=action_id,
        update_id=str(action.get("update_id") or action_id),
        handover="prepared" if accepted else "deferred",
    )


# ---------------------------------------------------------------------------
# system-modification kind (P21): model-dialogue-issued system changes.
#
# Bounded to two operations only — there is no generic "apply arbitrary
# change" surface:
#
#   config_value            edit one key in a governed settings JSON
#   codex_amendment_request emit a draft amendment request artifact
#   rollback_of             restore the before-snapshot of an executed
#                           config_value action
#
# Anything codex-bound becomes a request artifact (request-only authority),
# never a direct codex write.  Every execution records the material needed
# for rollback in the action result.
# ---------------------------------------------------------------------------

_CONFIG_ROOTS: tuple[str, ...] = (
    "main-system/config",
    "Standalone tools/local-model/runtime/settings",
)

_VALID_CHANGE_CLASSES: frozenset[str] = frozenset(
    {
        "editorial",
        "clarification",
        "provision-scope",
        "authority-duty",
        "architecture-authority",
        "complete-reconstitution",
    }
)

_AMENDMENT_REQUEST_DIR = (
    Path("governance_rule") / "execution" / "audit" / "convergence"
)


def _resolve_config_path(project_root: Path, rel: str) -> Path | None:
    candidate = (project_root / rel).resolve()
    try:
        candidate.relative_to(project_root.resolve())
    except ValueError:
        return None
    if candidate.suffix.lower() != ".json" or not candidate.is_file():
        return None
    normalized = candidate.relative_to(project_root.resolve()).as_posix()
    if not any(normalized.startswith(root + "/") for root in _CONFIG_ROOTS):
        return None
    return candidate


def _walk_key(document: Any, dotted: str) -> tuple[Any, bool]:
    node = document
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None, False
        node = node[part]
    return node, True


def _set_key(document: dict[str, Any], dotted: str, value: Any) -> Any:
    node = document
    parts = dotted.split(".")
    for part in parts[:-1]:
        node = node[part]
    before = node[parts[-1]]
    node[parts[-1]] = value
    return before


async def _execute_system_modification(
    app: Any, project_root: Path, action: dict[str, Any]
) -> dict[str, Any]:
    """Execute one confirmed system-modification action exactly once.

    Only the bounded operations above are legal; anything else fails
    closed.  The outcome always carries the material needed for rollback.
    """
    action_id = str(action.get("action_id") or "")
    detail = action.get("detail") or {}
    operation = str(detail.get("operation") or "")

    if operation == "config_value":
        return _apply_config_value(project_root, action_id, detail)
    if operation == "codex_amendment_request":
        return _emit_amendment_request(project_root, action_id, action, detail)
    if operation == "rollback_of":
        return _rollback_config_value(project_root, action_id, detail)
    return _result(
        False,
        error_code="SYSTEM_MODIFICATION_OPERATION_UNKNOWN",
        message=f"unknown system-modification operation: {operation!r}",
        action_id=action_id,
    )


def _apply_config_value(
    project_root: Path, action_id: str, detail: dict[str, Any]
) -> dict[str, Any]:
    rel = str(detail.get("path") or "")
    key = str(detail.get("key") or "")
    target = _resolve_config_path(project_root, rel)
    if target is None:
        return _result(
            False,
            error_code="CONFIG_PATH_NOT_ALLOWED",
            message=(
                "path must be an existing .json under a governed settings "
                "root (main-system/config, local-model runtime/settings)"
            ),
            action_id=action_id,
        )
    try:
        document = json.loads(target.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as error:
        return _result(
            False,
            error_code="CONFIG_UNREADABLE",
            message=f"{type(error).__name__}: {error}",
            action_id=action_id,
        )
    before, exists = _walk_key(document, key)
    if not exists:
        return _result(
            False,
            error_code="CONFIG_KEY_MISSING",
            message=(
                f"key {key!r} does not exist; system-modification only "
                "mutates existing keys (no schema creation)"
            ),
            action_id=action_id,
        )
    _set_key(document, key, detail.get("new_value"))
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(
        json.dumps(document, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    tmp.replace(target)
    return _result(
        True,
        action_id=action_id,
        decision="config-value-applied",
        result={
            "path": target.relative_to(project_root.resolve()).as_posix(),
            "key": key,
            "before_value": before,
            "after_value": detail.get("new_value"),
            "rollback": (
                "propose system-modification operation=rollback_of "
                f"with rollback_of={action_id}"
            ),
        },
    )


def _emit_amendment_request(
    project_root: Path,
    action_id: str,
    action: dict[str, Any],
    detail: dict[str, Any],
) -> dict[str, Any]:
    change_class = str(detail.get("change_class") or "")
    if change_class not in _VALID_CHANGE_CLASSES:
        return _result(
            False,
            error_code="AMENDMENT_CHANGE_CLASS_INVALID",
            message=(
                f"change_class must be one of "
                f"{sorted(_VALID_CHANGE_CLASSES)}"
            ),
            action_id=action_id,
        )
    slug = re.sub(r"[^a-z0-9]+", "-", str(detail.get("slug") or "dialogue"))[
        :48
    ].strip("-") or "dialogue"
    day = _iso_now()[:10].replace("-", "")
    amendment_id = f"codex-amendment-request-{slug}-{day}"
    directory = project_root / _AMENDMENT_REQUEST_DIR
    directory.mkdir(parents=True, exist_ok=True)
    artifact = directory / f"{amendment_id}.json"
    if artifact.exists():
        return _result(
            False,
            error_code="AMENDMENT_REQUEST_EXISTS",
            message=f"request artifact already exists: {artifact.name}",
            action_id=action_id,
        )
    request = {
        "artifact": "codex-amendment-request",
        "authority": "request-only",
        "amendment_id": amendment_id,
        "requested_by": "model-dialogue",
        "origin": str(detail.get("origin") or "model-dialogue system-modification"),
        "change_class": change_class,
        "required_review": "five-sovereign-audit-unanimous-pass",
        "flow": "A382/A488-non-disruptive-amendment-flow",
        "status": "draft-awaiting-governor",
        "not_executed": True,
        "problem": {"summary": str(action.get("summary") or "")},
        "request": detail.get("request") or {},
        "source_action_id": action_id,
    }
    artifact.write_text(
        json.dumps(request, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    return _result(
        True,
        action_id=action_id,
        decision="amendment-request-drafted",
        result={
            "artifact": artifact.relative_to(
                project_root.resolve()
            ).as_posix(),
            "status": "draft-awaiting-governor",
            "rollback": "delete the draft artifact (request-only; no codex state changed)",
        },
    )


def _rollback_config_value(
    project_root: Path, action_id: str, detail: dict[str, Any]
) -> dict[str, Any]:
    from .auto_action_policy import read_pending_actions

    source_id = str(detail.get("rollback_of") or "")
    source = next(
        (
            a
            for a in read_pending_actions(project_root)
            if a.get("action_id") == source_id
        ),
        None,
    )
    if source is None or source.get("status") != "executed":
        return _result(
            False,
            error_code="ROLLBACK_SOURCE_NOT_EXECUTED",
            message="rollback_of must reference an executed action",
            action_id=action_id,
        )
    result = source.get("result") or {}
    path = str(result.get("path") or "")
    key = str(result.get("key") or "")
    if "before_value" not in result or not path or not key:
        return _result(
            False,
            error_code="ROLLBACK_SNAPSHOT_MISSING",
            message="source action carries no before_value snapshot",
            action_id=action_id,
        )
    return _apply_config_value(
        project_root,
        action_id,
        {"path": path, "key": key, "new_value": result["before_value"]},
    )
