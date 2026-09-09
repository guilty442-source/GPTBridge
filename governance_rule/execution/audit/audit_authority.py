"""Authority, policy, identity, and permission checks for the governance audit."""

from __future__ import annotations

import json
import re
from pathlib import Path

from governance_rule.permission_directory.directory_authority import directory_authority_snapshot
from governance_rule.governance_policy import governance_policy_snapshot
from governance_rule.code_rule_directory import code_rule_directory_snapshot
from governance_rule.permission_directory.registries.permissions.capability_boundaries import (
    capability_boundary_snapshot,
)
from governance_rule.permission_directory.registries.permissions.identity_groups import (
    identity_group_snapshot,
)
from governance_rule.permission_directory.registries.permissions.identity_permissions import (
    identity_permission_snapshot,
)
from governance_rule.permission_directory.registries.permissions.source_ownership import (
    source_ownership_errors,
)
from governance_rule.codex import GOVERNANCE_CODEX


def check_authority_policy(root: Path, errors: list[str]) -> None:
    """Verify governance policy, directory authority, and code rule consistency."""
    policy = governance_policy_snapshot()
    code_rules = code_rule_directory_snapshot()
    directory = directory_authority_snapshot()

    if GOVERNANCE_CODEX.codex_version < 1.00000:
        errors.append("governance codex version must include the unified architecture policy")
    if policy.authority != "governance-codex-v1.00000-derived-enforcement-policy":
        errors.append("governance policy must remain the codex-v1.00000-derived enforcement projection")
    if policy.top_level_rule != "governance_codex" or policy.governance_rule_sources != (
        "governance_rule/codex/__init__.py",
    ):
        errors.append("governance codex must be the sole top-level rule source")
    if policy.governance_rule_count != 1:
        errors.append("governance rule count must equal one")
    if policy.governance_rule_partitioning:
        errors.append("governance rule partitioning must be disabled")
    if policy.subordinate_governance_rule_definition:
        errors.append("subordinate governance rule definition must be disabled")
    if policy.permission_hierarchy_role != "only-top-level-permission-authority":
        errors.append("governance policy must be the only top-level permission authority")
    if directory.permission_hierarchy_role != "subordinate-read-only-permission-directory":
        errors.append("directory authority must remain subordinate and read-only")
    if policy.authority_version != directory.authority_version_policy.current_version:
        errors.append("governance and directory authority versions do not match")
    if code_rules.authority_version != policy.authority_version:
        errors.append("code rule directory version does not match governance")
    if code_rules.governing_source != policy.governance_rule_sources[0]:
        errors.append("code rule directory is not governed by the single rule")
    if "codex-v1.00000-is-sole-rule-source" not in code_rules.requirements:
        errors.append("code rule directory does not declare the codex v1.00000 authority source")
    if code_rules.independent_authority or code_rules.runtime_write_allowed:
        errors.append("code rule directory must be subordinate and read-only")
    if code_rules.canonical_project_root != policy.code_architecture.all_source_code_root:
        errors.append("code rule directory canonical path does not match governance")
    if code_rules.shared_layer_root != policy.shared_layer.module_root:
        errors.append("shared layer path does not match governance")
    responsibilities = policy.system_responsibilities
    if (
        responsibilities.git != "system-version-and-development-history"
        or responsibilities.sql != "structured-mutable-official-data-postgresql"
        or responsibilities.sqlite
        != "owner-private-state-cache-checkpoint-or-bounded-reconciled-degraded-transport-only"
        or responsibilities.qdrant_rag != "qdrant-semantic-knowledge-index"
        or responsibilities.local_vector_fallback
        != "bounded-observable-degraded-cache-only-never-canonical"
        or responsibilities.llm != "understanding-reasoning-and-operations"
        or responsibilities.separation
        != "git-postgresql-sqlite-qdrant-rag-and-llm-roles-must-not-replace-one-another"
        or responsibilities.governed_flow
        != "llm-understands-reasons-and-operates-rag-retrieves-sql-persists-official-data-git-versions-system-changes"
        or responsibilities.management_owner
        != "xingcheng-core-orchestrator-under-governance-rule"
        or responsibilities.management_source_basis
        != (
            "governance-rule-for-authority-permissions-and-boundaries",
            "git-for-system-version-and-development-history",
            "sql-for-structured-mutable-official-data",
            "qdrant-semantic-knowledge-index-for-semantic-retrieval-candidates",
        )
        or responsibilities.llm_inference_as_source_of_truth
    ):
        errors.append("system responsibility architecture does not match governance")


def check_architecture_sources(root: Path, errors: list[str]) -> None:
    """Verify architecture source files declare the correct engine roles."""
    architecture_sources = {
        "shared_database": root / "shared-layer/src/shared_layer/database/__init__.py",
        "local_vector": root / "local-model/src/backend/services/xingcheng/infrastructure/vector_store.py",
        "market_network": root / "local-model/src/backend/services/xingcheng/infrastructure/market_data.py",
        "search_network": root / "local-model/src/backend/services/xingcheng/infrastructure/xingcheng_tools/search/searxng.py",
    }
    architecture_text = {
        name: path.read_text(encoding="utf-8") if path.is_file() else ""
        for name, path in architecture_sources.items()
    }
    if "POSTGRESQL_CANONICAL: bool = True" not in architecture_text["shared_database"]:
        errors.append("PostgreSQL must remain the canonical central structured data engine")
    if (
        '"engine": "local-vector-degraded-cache"' not in architecture_text["local_vector"]
        or '"canonical": False' not in architecture_text["local_vector"]
    ):
        errors.append("local vector storage must be declared as a non-canonical degraded cache")
    for source_name in ("market_network", "search_network"):
        if "NETWORK_DESTINATION_ALLOWLIST" not in architecture_text[source_name]:
            errors.append(f"governed network adapter lacks a static allowlist: {source_name}")


def check_third_party_inventory(root: Path, errors: list[str]) -> None:
    """Verify the third-party tool inventory declares correct formality."""
    inventory_path = root / "governance_rule/execution/third_party_management/tool_inventory.json"
    try:
        inventory = json.loads(inventory_path.read_text(encoding="utf-8"))
        inventory_tools = {item["id"]: item for item in inventory["tools"]}
    except (OSError, KeyError, TypeError, json.JSONDecodeError):
        inventory_tools = {}
        errors.append("third-party implementation inventory is invalid")
    for dependency_id in ("pybind11", "node", "npm", "uv"):
        dependency = inventory_tools.get(dependency_id, {})
        if dependency.get("formal") is not False or not str(
            dependency.get("formality") or ""
        ).startswith("approved-implementation-"):
            errors.append(f"implementation dependency is incorrectly authoritative: {dependency_id}")
    local_rag = inventory_tools.get("local-sqlite-rag", {})
    if local_rag.get("formal") is not False or local_rag.get("formality") != "bounded-degraded-fallback":
        errors.append("local SQLite RAG must remain a non-formal degraded fallback")


def check_shared_layer_policy(root: Path, errors: list[str]) -> None:
    """Verify shared layer access policy and channel isolation."""
    policy = governance_policy_snapshot()
    directory = directory_authority_snapshot()
    shared_policy = directory.shared_layer_access_policy
    if (
        shared_policy.module_root != policy.shared_layer.module_root
        or shared_policy.jurisdiction != "governance-policy-only"
        or shared_policy.main_system_module_member
        or not shared_policy.token_required
        or not shared_policy.database_only
        or shared_policy.source_write
        or shared_policy.direct_data_write
        or shared_policy.executable_content
        or shared_policy.direct_process_instruction
        or shared_policy.unchanneled_instruction != "PERMISSION_DENIED"
    ):
        errors.append("shared layer authority boundary is invalid")
    if policy.permission_distribution.authority_activation_order[0] != "governance-policy":
        errors.append("governance policy must activate before permission distribution")
    activation = policy.activation
    if (
        not activation.default_active
        or activation.activation_order
        != "before-main-system-and-all-other-modules"
        or not activation.direct_load_required
        or activation.independent_tool_packaging_exception
        != "governance_rule-only"
        or activation.packaged_executable_allowed
        or activation.execution_access != "read-only-direct-execution"
        or activation.encapsulation_allowed
        or activation.optional
        or activation.stop_permission
        or activation.disable_permission
        or activation.unload_permission
        or activation.lifetime != "entire-host-runtime"
        or activation.main_system_start_failure != "enter-automatic-repair"
        or activation.main_system_repair_authority != "governance-policy-only"
        or activation.main_system_repair_scope != "main-system-stability-only"
        or activation.repair_completion_gate
        != "governance-reverify-before-normal-mode"
    ):
        errors.append("governance must be directly loaded before main and cannot stop")
    if (
        not directory.governance_default_active
        or directory.governance_activation_order
        != activation.activation_order
        or not directory.governance_direct_load
        or directory.governance_packaging_exception
        != activation.independent_tool_packaging_exception
        or directory.governance_packaged_executable_allowed
        or directory.governance_execution_access != activation.execution_access
        or directory.governance_encapsulation_allowed
        or directory.governance_stop_permission
        or directory.governance_disable_permission
        or directory.governance_unload_permission
        or directory.main_system_start_failure
        != activation.main_system_start_failure
        or directory.main_system_repair_authority
        != activation.main_system_repair_authority
        or directory.main_system_repair_scope != activation.main_system_repair_scope
    ):
        errors.append("permission directory exposes an invalid governance lifecycle")
    if not policy.permission_distribution.all_registered_independent_tools_enabled:
        errors.append("registered independent tool permissions must be enabled")
    label_policy = policy.identifier_labels
    code_rules = code_rule_directory_snapshot()
    if label_policy.schema != code_rules.identifier_label_schema:
        errors.append("identifier label schema does not match code rule directory")
    if label_policy.aliases_allowed or label_policy.category_labels_allowed:
        errors.append("identifier aliases and category labels must be disabled")
    if code_rules.category_labels or directory.product_categories:
        errors.append("product category labels must not exist")
    if (
        shared_policy.database_path
        != "postgresql:gptbridge_transport:system"
        or shared_policy.ai_database_path
        != "postgresql:gptbridge_transport:ai"
        or shared_policy.database_path == shared_policy.ai_database_path
    ):
        errors.append("shared layer channel databases are not isolated")


def check_repair_policy(root: Path, errors: list[str]) -> None:
    """Verify automatic repair boundaries and backup assistance policy."""
    policy = governance_policy_snapshot()
    capability_boundaries, repair_boundaries = capability_boundary_snapshot()
    if not repair_boundaries:
        errors.append("automatic repair boundaries are missing")
    repair_policy = policy.automatic_repair
    if (
        not repair_policy.backup_assistance_allowed
        or repair_policy.backup_owner != "global-cleaner-only"
        or repair_policy.direct_backup_access
        or repair_policy.backup_request_channel
        != "governed-shared-layer-request-channel-only"
        or not repair_policy.authorization_per_step
        or repair_policy.authority_restore_from_backup
    ):
        errors.append("automatic repair backup assistance boundary is invalid")


def check_identity_permissions(root: Path, errors: list[str]) -> None:
    """Verify identity groups, permission bindings, and capability boundaries."""
    policy = governance_policy_snapshot()
    directory = directory_authority_snapshot()
    code_rules = code_rule_directory_snapshot()
    identity_group = identity_group_snapshot()
    permission_bindings = identity_permission_snapshot()
    capability_boundaries, _ = capability_boundary_snapshot()
    label_policy = policy.identifier_labels

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
    if any(
        binding.group_id != identity_group_by_actor.get(binding.actor)
        for binding in permission_bindings
    ):
        errors.append(
            "permission binding group must match the actor's dedicated group"
        )
    identity_actors = {identity.actor for identity in identity_group.identities}
    permission_actors = {binding.actor for binding in permission_bindings}
    if not permission_actors.issubset(identity_actors):
        errors.append("permission binding references an unknown identity")
    capability_names = [item.capability for item in capability_boundaries]
    capability_name_set = set(capability_names)
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
        "governance/tool/xingcheng",
        "governance/tool/star-chat",
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
        "governance/tool/star-chat",
    }
    if ai_process_actors != expected_ai_process_actors:
        errors.append("AI channel processing actors are invalid")
    actor_names = {identity.actor for identity in identity_group.identities}
    if actor_names != set(code_rules.approved_actor_names):
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
    for name in actor_names:
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


def check_main_system_source(root: Path, errors: list[str]) -> None:
    """Verify main system source does not reference legacy governance."""
    main_source = (root / "main-system/src-core/main.py").read_text(encoding="utf-8")
    if "GovernanceEnforcer" in main_source or "governance.enforcer" in main_source:
        errors.append("main system still references the legacy governance enforcer")
    if "MainSystemGovernance.from_environment" not in main_source:
        errors.append("main system is not launcher-attested")
    if "CoreLogger" in main_source:
        errors.append("main system must not implement persistent logging")

    runtime_source = (
        root / "main-system/src-core/core_system/governance_runtime.py"
    ).read_text(encoding="utf-8")
    if 'if tool_id == "governance_rule"' not in runtime_source:
        errors.append("governance lifecycle control is not explicitly denied")


def check_shared_layer_structure(root: Path, errors: list[str]) -> None:
    """Verify shared layer directory structure and readonly sources."""
    from .audit_protected import _is_operating_system_read_only

    policy = governance_policy_snapshot()
    shared_root = root / policy.shared_layer.module_root
    shared_source = root / policy.shared_layer.source_root
    shared_data = root / policy.shared_layer.data_root
    if not shared_root.is_dir() or not shared_source.is_dir() or not shared_data.is_dir():
        errors.append("shared layer independent module structure is incomplete")
    else:
        for candidate in (shared_root, shared_source, shared_data):
            if candidate.is_symlink():
                errors.append("shared layer paths must be physical directories")
        required_shared_sources = (
            shared_source / "shared_layer" / "__init__.py",
            shared_source / "shared_layer" / "channel.py",
            shared_source / "shared_layer" / "store.py",
        )
        for source in required_shared_sources:
            if not source.is_file():
                errors.append(f"shared layer source is missing: {source.name}")
            elif not _is_operating_system_read_only(source):
                errors.append(f"shared layer source is not read-only: {source.name}")
