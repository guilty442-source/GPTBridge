from __future__ import annotations

from pathlib import Path
from typing import Any, Dict


class PathScopeGuardRule:
    """
    Guard against path traversal and unsafe absolute-path operations.
    """

    rule_id = "path_scope_guard"
    reason = "target path is outside the allowed workspace scope"

    def evaluate(self, operation: Dict[str, Any]) -> bool:
        target = str(operation.get("target", "") or "").strip()
        destination = str(operation.get("destination", "") or "").strip()

        for raw in (target, destination):
            if not raw:
                continue
            # Block obvious traversal payloads.
            if ".." in raw.replace("\\", "/").split("/"):
                self.reason = f"unsafe relative path: {raw}"
                return False
            # Allow relative paths and Windows absolute drive paths.
            try:
                path = Path(raw)
                if path.is_absolute() and len(path.parts) == 1:
                    self.reason = f"invalid absolute path: {raw}"
                    return False
            except Exception:
                self.reason = f"invalid path: {raw}"
                return False

        return True

