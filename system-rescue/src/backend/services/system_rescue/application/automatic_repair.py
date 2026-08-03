from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from ..domain.contracts import SYSTEM_RESCUE_VERSION
from ..domain.repair_policy import plan_repair
from ..infrastructure.database_recovery import DatabaseRecoveryInspector
from ..infrastructure.repair_run_store import RepairRunStore


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _inside(candidate: Path, root: Path) -> bool:
    try:
        candidate.resolve(strict=False).relative_to(root.resolve())
        return True
    except (OSError, RuntimeError, ValueError):
        return False


class CentralAutomaticRepairService:
    """The single automatic-repair program governed by System Rescue."""

    VERSION = SYSTEM_RESCUE_VERSION

    def __init__(
        self,
        project_root: Path,
        tool_root: Path,
        *,
        package_rebuilder: Callable[[str], dict[str, Any]] | None = None,
    ) -> None:
        self.project_root = project_root.resolve()
        self.tool_root = tool_root.resolve()
        self.database_root = self.tool_root / "data" / "automatic-repair"
        self.store = RepairRunStore(self.database_root)
        self.package_rebuilder = package_rebuilder

    def _target_root(self, target_tool_id: str) -> tuple[str, Path]:
        target_id = str(target_tool_id or "").strip()
        if (
            not target_id
            or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for character in target_id)
            or target_id in {"governance-rule", "system-rescue"}
        ):
            raise PermissionError("PERMISSION_DENIED")
        target_root = (self.project_root / target_id).resolve()
        manifest_path = target_root / "manifest.json"
        if target_id == "main-system":
            if not (target_root / "package.json").is_file():
                raise PermissionError("PERMISSION_DENIED")
        elif target_id == "shared-layer":
            target_root = (self.project_root / "shared-layer").resolve()
            if not (target_root / "src" / "shared_layer" / "store.py").is_file():
                raise PermissionError("PERMISSION_DENIED")
        else:
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as error:
                raise PermissionError("PERMISSION_DENIED") from error
            if str(manifest.get("id") or "") != target_id:
                raise PermissionError("PERMISSION_DENIED")
        if (
            target_id != "shared-layer"
            and target_root.parent != self.project_root
        ) or not _inside(target_root, self.project_root):
            raise PermissionError("PERMISSION_DENIED")
        return target_id, target_root

    @staticmethod
    def _sqlite_candidates(target_root: Path) -> list[Path]:
        return DatabaseRecoveryInspector.sqlite_candidates(target_root)

    def repair_tool(self, target_tool_id: str, failure_code: str) -> dict[str, Any]:
        requester = str(os.environ.get("GPTBRIDGE_GOVERNED_REQUESTER_ACTOR") or "").strip()
        if requester != "governance/main-system":
            raise PermissionError("PERMISSION_DENIED")
        target_id, target_root = self._target_root(target_tool_id)
        plan = plan_repair(failure_code)
        run_id = uuid.uuid4().hex
        started_at = _iso_now()
        inspection = DatabaseRecoveryInspector(
            self.project_root,
            target_root,
        ).inspect()
        errors = list(inspection["database_errors"])
        package_repair: dict[str, Any] = {
            "triggered": False,
            "owner": "system-rescue",
            "reason": "PACKAGE_REBUILD_NOT_REQUIRED",
        }
        executed_actions = ["inspect-owned-databases"]
        if plan.rebuild_executable:
            if self.package_rebuilder is None:
                package_repair = {
                    "triggered": True,
                    "ok": False,
                    "owner": "system-rescue",
                    "error_code": "PACKAGE_REBUILDER_UNAVAILABLE",
                }
            else:
                package_repair = {
                    "triggered": True,
                    **self.package_rebuilder(target_id),
                }
            executed_actions.append("rebuild-tool-executable")
            if package_repair.get("ok") is not True:
                errors.append(
                    str(package_repair.get("error_code") or "PACKAGE_REBUILD_FAILED")
                )

        result: dict[str, Any] = {
            "ok": not errors,
            "operation": "central-automatic-repair",
            "authority": "system-rescue",
            "version": self.VERSION,
            "run_id": run_id,
            "target_tool_id": target_id,
            "failure_code": plan.failure_code,
            "repair_plan": plan.as_dict(),
            "executed_actions": executed_actions,
            **inspection,
            "package_repair": package_repair,
            "direct_backup_access": False,
            "errors": errors,
            "started_at": started_at,
            "completed_at": _iso_now(),
        }
        result["database"] = str(self.store.record(target_id, result))
        return result
