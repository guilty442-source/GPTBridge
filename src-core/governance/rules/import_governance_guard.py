from __future__ import annotations

import re
from typing import Any, Dict


class ImportGovernanceGuard:
    """Basic import validity checks for TS/TSX source updates."""

    rule_id = "import_governance_guard"

    IMPORT_PATTERN = re.compile(r"from\\s+['\"]([^'\"]+)['\"]")

    def __init__(self) -> None:
        self.reason = ""
        self.auto_fix_result = ""

    def evaluate(self, operation: Dict[str, Any]) -> bool:
        action = str(operation.get("action", ""))
        target = str(operation.get("target", ""))
        content = str(operation.get("content", ""))

        if action not in {"create_file", "modify_file"}:
            return True
        if not target.endswith((".ts", ".tsx", ".js", ".jsx")):
            return True

        for line_no, line in enumerate(content.splitlines(), start=1):
            match = self.IMPORT_PATTERN.search(line)
            if not match:
                continue

            import_path = match.group(1)
            if import_path.startswith("../../../../"):
                self.reason = f"import path too deep at line {line_no}: {import_path}"
                self.auto_fix_result = "prefer '@/...' alias or local relative path"
                return False

        return True
