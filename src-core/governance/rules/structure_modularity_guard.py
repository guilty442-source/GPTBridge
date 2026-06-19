from __future__ import annotations

from typing import Any, Dict


class StructureModularityGuard:
    """Guard coarse module boundaries in renderer source."""

    rule_id = "structure_modularity_guard"

    def __init__(self) -> None:
        self.reason = ""
        self.auto_fix_result = "move shared logic into src-ui/renderer/shared"

    def evaluate(self, operation: Dict[str, Any]) -> bool:
        target = str(operation.get("target", ""))
        content = str(operation.get("content", ""))

        normalized = target.replace('\\', '/')
        if "src-ui/renderer/" not in normalized:
            return True

        if "/toolbox/" in normalized and "@/ui/developer-mode" in content:
            self.reason = "toolbox module cannot directly depend on developer-mode internals"
            return False

        if "/app/" in normalized and "../.." in content and "@/" not in content:
            self.reason = "prefer alias import in app layer to keep path stability"
            return False

        return True
