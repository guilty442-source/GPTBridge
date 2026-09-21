"""Audit authority helpers (A185 split).

Contains the identity validation and code rules validation helpers
extracted from check_identity_permissions.
"""
from __future__ import annotations

import re
from typing import Any


def _validate_identity_groups(
    identity_group: Any,
    directory: Any,
    errors: list[str],
) -> dict[str, Any]:
    """Validate identity groups and return derived data.

    Returns a dict with: identity_group_by_actor, identity_actors.
    """
    if identity_group.group_id != directory.active_identity_group_id:
        errors.append("active identity group does not match directory authority")
    active_group_ids = set(directory.active_identity_group_ids)
    identity_group_ids = [item.group_id for item in identity_group.identities]
    identity_codes = [item.identity_code for item in identity_group.identities]
    identity_language_names = [
        item.language_name for item in identity_group.identities
    ]
    identity_codenames = [item.codename for item in identity_group.identities]
    if any(gid not in active_group_ids for gid in identity_group_ids):
        errors.append("identity references a group outside the active set")
    for label, values in (
        ("identity group", identity_group_ids),
        ("identity code", identity_codes),
        ("identity language name", identity_language_names),
        ("identity codename", identity_codenames),
    ):
        if len(values) != len(set(values)):
            errors.append(f"{label} assignments contain duplicates")
    if any(
        not re.fullmatch(r"[A-Z][0-9]{5}", code) for code in identity_codes
    ):
        errors.append("identity code must be one letter plus five digits")
    if any(
        not re.fullmatch(r"[a-z][a-z0-9_]*", name)
        for name in identity_language_names
    ):
        errors.append("identity language name must be a code identifier")
    if any(not name.strip() for name in identity_codenames):
        errors.append("identity codename must not be empty")
    identity_group_by_actor = {
        item.actor: item.group_id for item in identity_group.identities
    }
    identity_actors = {identity.actor for identity in identity_group.identities}
    return {
        "identity_group_by_actor": identity_group_by_actor,
        "identity_actors": identity_actors,
    }


def _validate_permission_bindings(
    permission_bindings: Any,
    identity_group_by_actor: dict[str, Any],
    identity_actors: set,
    capability_boundaries: Any,
    errors: list[str],
) -> None:
    """Validate permission bindings and capability boundaries."""
    if any(
        binding.group_id != identity_group_by_actor.get(binding.actor)
        for binding in permission_bindings
    ):
        errors.append(
            "permission binding group must match the actor's dedicated group"
        )
    permission_actors = {binding.actor for binding in permission_bindings}
    if not permission_actors.issubset(identity_actors):
        errors.append("permission binding references an unknown identity")
    capability_names = [item.capability for item in capability_boundaries]
    assigned_capabilities = [
        capability
        for binding in permission_bindings
        for capability in binding.capabilities
    ]
    if len(capability_names) != len(set(capability_names)):
        errors.append("capability boundaries contain duplicates")
    if not set(assigned_capabilities).issubset(set(capability_names)):
        errors.append("identity permission references an unknown capability")
    if any(
        item.capability == "shared-layer-read-write"
        for item in capability_boundaries
    ) or any(
        "shared-layer-read-write" in binding.capabilities
        for binding in permission_bindings
    ):
        errors.append("shared layer must not expose arbitrary record storage")


def _validate_channel_actors(
    permission_bindings: Any,
    identity_actors: set,
    errors: list[str],
) -> None:
    """Validate system channel and AI channel actor sets."""
    shared_submit_actors = {
        binding.actor
        for binding in permission_bindings
        if "system-channel-request-submit" in binding.capabilities
    }
    if shared_submit_actors != identity_actors:
        errors.append("system channel submission must cover every registered identity")
    shared_process_actors = {
        binding.actor
        for binding in permission_bindings
        if "system-channel-request-process" in binding.capabilities
    }
    expected_process_actors = identity_actors - {"governance/main-system"}
    if shared_process_actors != expected_process_actors:
        errors.append("system channel processing must cover only independent tools")
    ai_submit_actors = {
        binding.actor
        for binding in permission_bindings
        if "ai-channel-request-submit" in binding.capabilities
    }
    expected_ai_submit_actors = {
        "governance/tool/ai-assistant",
        "governance/tool/ai-collaboration",
        "governance/tool/investment-mobile",
        "governance/tool/model-dialogue",
        "governance/tool/xingcheng",
        "governance/tool/chinese-semantic-engine",
    }
    if ai_submit_actors != expected_ai_submit_actors:
        errors.append("AI channel submission actors are invalid")
    ai_process_actors = {
        binding.actor
        for binding in permission_bindings
        if "ai-channel-request-process" in binding.capabilities
    }
    expected_ai_process_actors = expected_ai_submit_actors - {
        "governance/tool/investment-mobile",
        "governance/tool/model-dialogue",
    }
    if ai_process_actors != expected_ai_process_actors:
        errors.append("AI channel processing actors are invalid")


def _validate_code_rules(
    identity_actors: set,
    capability_name_set: set,
    capability_boundaries: Any,
    code_rules: Any,
    label_policy: Any,
    errors: list[str],
) -> None:
    """Validate code rules against identity and capability data."""
    # Companion tools (e.g. star-chat) share their owner's approved actor
    # name and are not separately listed in the approved actor name list.
    _COMPANION_ACTORS = frozenset()
    if (identity_actors - _COMPANION_ACTORS) != set(code_rules.approved_actor_names):
        errors.append("identity actors do not match the approved name list")
    if capability_name_set - set(code_rules.approved_capability_names):
        errors.append("permission capability is not in the approved name list")
    action_names = {
        grant.action
        for capability in capability_boundaries
        for grant in capability.grants
    }
    target_names = {
        grant.target
        for capability in capability_boundaries
        for grant in capability.grants
    }
    data_scope_names = {
        grant.data_scope
        for capability in capability_boundaries
        for grant in capability.grants
    }
    if action_names - set(code_rules.approved_action_names):
        errors.append("actions do not match the approved name list")
    if target_names - set(code_rules.approved_target_names):
        errors.append("targets do not match the approved name list")
    if data_scope_names - set(code_rules.approved_data_scope_names):
        errors.append("data scopes do not match the approved name list")
    for name in identity_actors:
        if re.fullmatch(label_policy.actor_pattern, name) is None:
            errors.append(f"actor name is not standardized: {name}")
    for name in capability_name_set:
        if re.fullmatch(label_policy.capability_pattern, name) is None:
            errors.append(f"capability name is not standardized: {name}")
    for name in action_names:
        if re.fullmatch(label_policy.action_pattern, name) is None:
            errors.append(f"action name is not standardized: {name}")
    for name in target_names:
        if re.fullmatch(label_policy.target_pattern, name) is None:
            errors.append(f"target name is not standardized: {name}")
    for name in data_scope_names:
        if re.fullmatch(label_policy.data_scope_pattern, name) is None:
            errors.append(f"data scope name is not standardized: {name}")


__all__ = [
    "_validate_identity_groups",
    "_validate_permission_bindings",
    "_validate_channel_actors",
    "_validate_code_rules",
]
