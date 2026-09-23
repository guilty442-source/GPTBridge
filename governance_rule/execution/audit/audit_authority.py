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
from governance_rule.execution.codex_repository import (
    CODEX_VERSION_UNIT,
    load_governance_codex,
)
from .audit_authority_helpers import (
    _validate_identity_groups,
    _validate_permission_bindings,
    _validate_channel_actors,
    _validate_code_rules,
)


def check_authority_policy(root: Path, errors: list[str]) -> None:
    """Verify governance policy, directory authority, and code rule consistency."""
    policy = governance_policy_snapshot()
    code_rules = code_rule_directory_snapshot()
    directory = directory_authority_snapshot()

    try:
        codex_version = load_governance_codex().codex_version
    except Exception as error:
        codex_version = None
        errors.append(f"governance codex authority is unreadable: {error}")
    if codex_version is not None and codex_version < CODEX_VERSION_UNIT:
        errors.append("governance codex version must include the unified architecture policy")
    if policy.authority != "governance-codex-v1.32010-derived-enforcement-policy":
        errors.append("governance policy must remain the codex-v1.32010-derived enforcement projection")
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
    if "codex-v1.32010-is-sole-rule-source" not in code_rules.requirements:
        errors.append("code rule directory does not declare the codex v1.32010 authority source")
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
        "local_vector": root / "shared-layer/src/shared_layer/local/vector_store.py",
        "market_network": root / "Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/market_data.py",
        "search_network": root / "Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/xingcheng_tools/search/searxng.py",
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
        or repair_policy.backup_owner != "system-rescue-only"
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

    identity_data = _validate_identity_groups(identity_group, directory, errors)
    identity_group_by_actor = identity_data["identity_group_by_actor"]
    identity_actors = identity_data["identity_actors"]
    _validate_permission_bindings(
        permission_bindings, identity_group_by_actor, identity_actors,
        capability_boundaries, errors,
    )
    _validate_channel_actors(permission_bindings, identity_actors, errors)
    capability_name_set = {item.capability for item in capability_boundaries}
    _validate_code_rules(
        identity_actors, capability_name_set, capability_boundaries,
        code_rules, label_policy, errors,
    )

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


def check_tool_isolation_hardening(root: Path, errors: list[str]) -> None:
    """Verify tool isolation source contains A266 hardening controls (A266/A121).

    Checks that the tool isolation manager and spawn path enforce:
    - stdin isolation (DEVNULL) — tools cannot read parent stdin.
    - close_fds — file descriptors do not leak to child processes.
    - job assignment verification — failed assignment is audited.
    - durable isolation audit ledger — registrations are recorded.
    """
    isolation_source = (
        root / "main-system/src-core/core_system/tool_isolation.py"
    ).read_text(encoding="utf-8")
    spawn_source = chr(10).join(
        (root / "main-system/src-core/tasks" / name).read_text(encoding="utf-8")
        for name in (
            "toolbox_start_spawn.py",
            "toolbox_start_spawn_process.py",
        )
        if (root / "main-system/src-core/tasks" / name).is_file()
    )
    required_isolation_controls = (
        ("_record_isolation_audit", "durable isolation audit ledger"),
        ("job_assigned", "job assignment verification"),
        ("job-assignment-failed", "job assignment failure audit"),
    )
    for marker, label in required_isolation_controls:
        if marker not in isolation_source:
            errors.append(f"tool isolation lacks {label}")
    required_spawn_controls = (
        ("stdin=subprocess.DEVNULL", "spawn stdin isolation"),
        ("close_fds=True", "spawn close_fds"),
    )
    for marker, label in required_spawn_controls:
        if marker not in spawn_source:
            errors.append(f"tool spawn lacks {label}")
