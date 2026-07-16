from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from core_system.boundaries import SYSTEM_BOUNDARIES, boundary_roots


class SystemBoundaryGuardRule:
    """Enforce the declared main-program and independent-tool write boundaries."""

    rule_id = "system_boundary_guard"
    reason = "actor is not allowed to modify the requested system boundary"
    _MUTATING_ACTIONS = {
        "create_file",
        "create_folder",
        "modify_file",
        "delete_file",
        "delete_folder",
        "move_file",
    }
    _MAIN_ACTORS = {"core", "main_program", "toolbox"}

    def __init__(self, project_root: Path) -> None:
        self.project_root = Path(project_root).resolve()
        self.boundaries = {item.key: item for item in SYSTEM_BOUNDARIES}

    @staticmethod
    def _is_within(candidate: Path, roots: list[Path]) -> bool:
        for root in roots:
            try:
                candidate.relative_to(root)
                return True
            except ValueError:
                continue
        return False

    def evaluate(self, operation: Dict[str, Any]) -> bool:
        action = str(operation.get("action", "")).strip()
        actor = str(operation.get("actor", "")).strip().lower()
        if action not in self._MUTATING_ACTIONS or actor not in self._MAIN_ACTORS:
            return True

        targets = [operation.get("target"), operation.get("destination")]
        main = self.boundaries["main_program"]
        tools = self.boundaries["independent_tools"]
        main_roots = boundary_roots(self.project_root, "main_program")
        tool_roots = boundary_roots(self.project_root, "independent_tools")

        for raw_target in targets:
            if not raw_target:
                continue
            target = Path(str(raw_target))
            if not target.is_absolute():
                target = self.project_root / target
            target = target.resolve(strict=False)
            if self._is_within(target, main_roots) and not main.may_modify_main_program:
                self.reason = f"{actor} cannot modify main-program code: {target}"
                return False
            if self._is_within(target, tool_roots) and not main.may_modify_independent_tools:
                self.reason = f"{actor} cannot modify independent-tool code: {target}"
                return False
        return True
