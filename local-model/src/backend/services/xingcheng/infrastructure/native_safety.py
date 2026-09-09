from __future__ import annotations

import re
from typing import Any, Mapping, Sequence


class StarNativeSafetyMixin:
    """Context completion and command-safety evaluation."""

    @classmethod
    def _context_completion(
        cls,
        *,
        command: str,
        context: str,
        actions: Sequence[Mapping[str, Any]],
        objects: Sequence[Mapping[str, Any]],
        parameters: Mapping[str, Any],
    ) -> dict[str, Any]:
        command_lower = command.casefold()
        references = [
            marker for marker in cls._REFERENCE_MARKERS if marker in command_lower
        ]
        generic_only = bool(
            re.fullmatch(
                r"\s*(?:請|麻煩|幫我|我已|我)?\s*(?:確認執行|確認刪除|執行|繼續|照做|開始|停止|刪掉|刪除|修改|處理)(?:吧|一下|即可|。|！|!)?\s*",
                command,
                flags=re.IGNORECASE,
            )
        )
        needs_context = bool(references or generic_only)
        context_text = str(context or "").strip()
        context_actions = cls._extract_command_actions(context_text) if context_text else []
        context_objects = cls._extract_operation_objects(context_text) if context_text else []
        context_parameters = (
            cls._extract_command_parameters(context_text) if context_text else {}
        )
        supplied = bool(context_text and needs_context)
        resolved = bool(
            supplied
            and (objects or context_objects)
            and (
                any(parameters.get(key) for key in ("paths", "filenames", "model_names"))
                or any(
                    context_parameters.get(key)
                    for key in ("paths", "filenames", "model_names")
                )
                or not any(
                    str(item.get("action") or "") == "delete" for item in actions
                )
            )
        )
        resolved_command = command
        if supplied:
            resolved_command = (
                f"{command}\n\n可用的前文補全依據：\n{context_text[-8_000:]}"
            )
        return {
            "needed": needs_context,
            "attempted": supplied,
            "resolved": resolved if needs_context else True,
            "reference_markers": references,
            "generic_command": generic_only,
            "context_actions": context_actions,
            "context_objects": context_objects,
            "context_parameters": context_parameters,
            "resolved_command": resolved_command,
            "source": "recent-conversation" if supplied else "none",
        }

    @classmethod
    def _command_safety(
        cls,
        *,
        command: str,
        actions: Sequence[Mapping[str, Any]],
        objects: Sequence[Mapping[str, Any]],
        parameters: Mapping[str, Any],
        context_completion: Mapping[str, Any],
        intent_explicit: bool,
        confirmed: bool,
    ) -> dict[str, Any]:
        lowered = command.casefold()
        context_actions = context_completion.get("context_actions")
        if not isinstance(context_actions, Sequence):
            context_actions = []
        context_objects = context_completion.get("context_objects")
        if not isinstance(context_objects, Sequence):
            context_objects = []
        effective_objects = [
            item
            for item in (*objects, *context_objects)
            if isinstance(item, Mapping)
        ]
        destructive = bool(
            "delete" in {str(item.get("action") or "") for item in actions}
            or any(marker in lowered for marker in cls._DESTRUCTIVE_MARKERS)
            or (
                context_completion.get("needed") is True
                and "delete"
                in {
                    str(item.get("action") or "")
                    for item in context_actions
                    if isinstance(item, Mapping)
                }
            )
        )
        context_parameters = context_completion.get("context_parameters")
        if not isinstance(context_parameters, Mapping):
            context_parameters = {}
        has_specific_target = bool(
            any(parameters.get(key) for key in ("paths", "filenames", "model_names"))
            or any(
                context_parameters.get(key)
                for key in ("paths", "filenames", "model_names")
            )
        )
        understood = bool(actions or intent_explicit)
        understanding_failed = bool(
            not understood
            or (
                context_completion.get("needed") is True
                and context_completion.get("resolved") is not True
            )
            or (destructive and (not effective_objects or not has_specific_target))
        )
        remaining_ambiguities: list[str] = []
        if not understood:
            remaining_ambiguities.append("action-or-intent-not-understood")
        if context_completion.get("needed") is True and context_completion.get("resolved") is not True:
            remaining_ambiguities.append("context-reference-unresolved")
        if destructive and not effective_objects:
            remaining_ambiguities.append("destructive-object-not-identified")
        if destructive and not has_specific_target:
            remaining_ambiguities.append("destructive-target-not-specific")
        confirmation_accepted = bool(
            confirmed and has_specific_target and effective_objects
        )
        confirmation_required = bool(
            destructive and understanding_failed and not confirmation_accepted
        )
        return {
            "destructive_operation": destructive,
            "understanding_failed": understanding_failed,
            "context_completion_attempted": context_completion.get("attempted") is True,
            "remaining_ambiguities": list(dict.fromkeys(remaining_ambiguities)),
            "confirmation_provided": confirmed,
            "confirmation_accepted": confirmation_accepted,
            "confirmation_required": confirmation_required,
            "operation_allowed": not confirmation_required,
            "decision": "safe-confirmation-required"
            if confirmation_required
            else "continue-under-governance",
        }
