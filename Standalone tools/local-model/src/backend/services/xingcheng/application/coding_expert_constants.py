from __future__ import annotations

import json
import re
from typing import Any


class CodingExpertConstants:
    """Class-level constants and low-level utility helpers for StarCodingExpert."""

    ALLOWED_LANGUAGES = frozenset({"python", "typescript", "javascript", "sql", "json"})
    ALLOWED_ACTIONS = frozenset({"generate", "analyze", "refactor", "self_upgrade"})
    ALLOWED_PYTHON_KINDS = frozenset(
        {"function", "class", "dataclass", "test", "api"}
    )
    ALLOWED_SCRIPT_KINDS = frozenset({"function", "class", "test"})
    PROTECTED_PROJECT_ROOTS = frozenset({"governance_rule", ".git"})
    PROJECT_SOURCE_ROOTS = frozenset(
        {
            "ai-assistant",
            "ai-collaboration",
            "file-sorter",
            "global-cleaner",
            "investment-mobile",
            "xingcheng",
            "main-system",
            "shared-layer",
            "main-system-central-repair",
            "vaultly",
        }
    )
    PROHIBITED_IMPORTS = frozenset({"ctypes", "subprocess", "winreg"})
    PROHIBITED_CALLS = frozenset(
        {
            "eval",
            "exec",
            "compile",
            "__import__",
            "builtins.eval",
            "builtins.exec",
            "builtins.compile",
            "builtins.__import__",
            "os.system",
            "os.remove",
            "os.unlink",
            "os.rmdir",
            "os.removedirs",
            "os.rename",
            "os.replace",
            "shutil.rmtree",
            "shutil.move",
            "pathlib.Path.unlink",
            "pathlib.Path.rmdir",
            "pathlib.Path.rename",
            "pathlib.Path.replace",
            "pathlib.Path.write_text",
            "pathlib.Path.write_bytes",
            # Calls on constructed Path objects have no stable AST prefix.
            "unlink",
            "rmdir",
            "write_text",
            "write_bytes",
        }
    )
    PROHIBITED_DYNAMIC_CALL_NAMES = frozenset(
        {
            "eval",
            "exec",
            "compile",
            "__import__",
            "system",
            "remove",
            "unlink",
            "rmdir",
            "removedirs",
            "rename",
            "replace",
            "rmtree",
            "move",
            "write_text",
            "write_bytes",
        }
    )
    PROHIBITED_SCRIPT_PATTERNS = (
        r"\beval\s*\(",
        r"\bnew\s+Function\s*\(",
        r"\bchild_process\b",
        r"\bprocess\.binding\s*\(",
    )
    PROHIBITED_SQL_KEYWORDS = frozenset(
        {
            "INSERT",
            "UPDATE",
            "DELETE",
            "DROP",
            "ALTER",
            "CREATE",
            "REPLACE",
            "ATTACH",
            "DETACH",
            "PRAGMA",
        }
    )

    @staticmethod
    def _identifier(value: Any, default: str = "generated_task") -> str:
        normalized = re.sub(r"\W+", "_", str(value or "").strip(), flags=re.UNICODE)
        normalized = normalized.strip("_") or default
        if normalized[0].isdigit():
            normalized = f"task_{normalized}"
        return normalized[:80] if normalized.isidentifier() else default

    @staticmethod
    def _string_literal(value: Any) -> str:
        return json.dumps(str(value or ""), ensure_ascii=False)
