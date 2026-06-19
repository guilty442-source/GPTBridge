from __future__ import annotations

import re
from typing import Any, Dict


class GeminiCodeAssistLockdownRule:
    """Hard-block write operations from Gemini Code Assist actors."""

    rule_id = "gemini_code_assist_lockdown"
    reason = "Gemini Code Assist is not allowed to modify project files"

    WRITE_ACTIONS = {
        "create_file",
        "modify_file",
        "delete_file",
        "delete_folder",
        "move_file",
        "modify_module",
        "modify_workflow",
    }

    EXACT_ACTOR_ALIASES = {
        "gemini_code_assist",
        "gemini-code-assist",
        "gemini code assist",
        "google_gemini_code_assist",
        "google-gemini-code-assist",
        "google gemini code assist",
    }

    @staticmethod
    def _normalize_actor(actor: str) -> str:
        lowered = actor.strip().lower()
        normalized = re.sub(r"[^a-z0-9]+", " ", lowered).strip()
        return normalized

    def _is_gemini_code_assist_actor(self, actor: str) -> bool:
        raw = actor.strip().lower()
        normalized = self._normalize_actor(actor)
        tokens = set(normalized.split())

        if raw in self.EXACT_ACTOR_ALIASES or normalized in self.EXACT_ACTOR_ALIASES:
            return True

        return (
            "gemini" in tokens
            and "assist" in tokens
            and ("code" in tokens or "coder" in tokens)
        )

    @staticmethod
    def _is_write_action(action: str) -> bool:
        if action in GeminiCodeAssistLockdownRule.WRITE_ACTIONS:
            return True
        return action.startswith(("create_", "modify_", "delete_", "move_", "rename_", "write_", "patch_"))

    def evaluate(self, operation: Dict[str, Any]) -> bool:
        actor = str(operation.get("actor", "") or "")
        action = str(operation.get("action", "") or "").strip().lower()
        target = str(operation.get("target", "") or "")

        if not self._is_gemini_code_assist_actor(actor):
            return True

        if self._is_write_action(action):
            target_text = target if target else "(unknown target)"
            self.reason = (
                f"blocked actor '{actor}' from write action '{action}' on '{target_text}'"
            )
            return False

        return True
