from __future__ import annotations

import re
from typing import Any, Dict


class NoPlaceholderPollutionRule:
    """Block obvious placeholder-only implementations in created/modified files."""

    rule_id = "no_placeholder_pollution"
    reason = "placeholder implementation detected"

    PLACEHOLDER_PATTERNS = (
        re.compile(r"TODO:\\s*implement", re.IGNORECASE),
        re.compile(r"raise\\s+NotImplementedError", re.IGNORECASE),
    )

    def evaluate(self, operation: Dict[str, Any]) -> bool:
        action = str(operation.get("action", ""))
        content = str(operation.get("content", ""))

        if action not in {"create_file", "modify_file"}:
            return True

        if not content.strip():
            self.reason = "empty file content is not allowed"
            return False

        for pattern in self.PLACEHOLDER_PATTERNS:
            if pattern.search(content):
                self.reason = f"placeholder pattern detected: {pattern.pattern}"
                return False

        return True
