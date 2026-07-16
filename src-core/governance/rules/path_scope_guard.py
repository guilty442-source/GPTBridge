from __future__ import annotations

from pathlib import Path
from typing import Any, Dict


class PathScopeGuardRule:
    """
    Guard against path traversal and unsafe absolute-path operations.
    """

    rule_id = "path_scope_guard"
    reason = "target path is outside the allowed workspace scope"

    def __init__(self, project_root: Path) -> None:
        self.project_root = Path(project_root).resolve()

    def _inside_project(self, raw: str) -> bool:
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = self.project_root / candidate
        try:
            resolved = candidate.resolve(strict=False)
            resolved.relative_to(self.project_root)
            return True
        except (OSError, RuntimeError, ValueError):
            return False

    def evaluate(self, operation: Dict[str, Any]) -> bool:
        target = str(operation.get("target", "") or "").strip()
        destination = str(operation.get("destination", "") or "").strip()

        for raw in (target, destination):
            if not raw:
                continue
            if not self._inside_project(raw):
                self.reason = f"path is outside project root: {raw}"
                return False

        return True
