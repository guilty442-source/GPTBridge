from __future__ import annotations

import ast
import json
import re
import stat
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


PROJECT_ROOT = Path(__file__).resolve().parents[3]
FORBIDDEN_LEGACY_SOURCES = (
    "main-system/src-core/core_logger.py",
    "main-system/src-core/core_system/boundaries.py",
    "main-system/src-core/governance",
    "main-system/scripts/audit_runtime_governance.py",
    "platform_tools",
)

REQUIRED_GOVERNANCE_ENFORCEMENT_SOURCES = frozenset(
    {
        "governance_rule/execution/authentication/__init__.py",
        "governance_rule/execution/integrity/__init__.py",
        "governance_rule/execution/versioning/__init__.py",
        "governance_rule/permission_directory/execution/identity_registry/__init__.py",
        "governance_rule/permission_directory/execution/path_guard/__init__.py",
        "main-system/src-ui/main/governance-bootstrap.ts",
        "main-system/src-core/core_system/governance_runtime.py",
    }
)


def _is_operating_system_read_only(path: Path) -> bool:
    attributes = int(getattr(path.stat(), "st_file_attributes", 0) or 0)
    read_only_flag = int(getattr(stat, "FILE_ATTRIBUTE_READONLY", 0) or 0)
    return bool(read_only_flag and attributes & read_only_flag)


def audit_runtime_governance(project_root: Path = PROJECT_ROOT) -> list[str]:
    root = project_root.resolve()
    errors: list[str] = []
    policy = governance_policy_snapshot()
    code_rules = code_rule_directory_snapshot()
    directory = directory_authority_snapshot()
    identity_group = identity_group_snapshot()
    permission_bindings = identity_permission_snapshot()
    capability_boundaries, repair_boundaries = capability_boundary_snapshot()
    errors.extend(source_ownership_errors(root))

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
        or responsibilities.qdrant_rag != "qdrant-semantic-knowledge-index"
        or responsibilities.llm != "understanding-reasoning-and-operations"
        or responsibilities.separation
        != "git-sql-rag-and-llm-must-not-replace-one-another"
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
    if label_policy.schema != code_rules.identifier_label_schema:
        errors.append("identifier label schema does not match code rule directory")
    if label_policy.aliases_allowed or label_policy.category_labels_allowed:
        errors.append("identifier aliases and category labels must be disabled")
    if code_rules.category_labels or directory.product_categories:
        errors.append("product category labels must not exist")

    protected_sources = (
        *policy.authority_files,
        *directory.managed_read_only_registry_paths,
    )
    if len(protected_sources) != len(set(protected_sources)):
        errors.append("protected governance sources contain duplicates")
    if not REQUIRED_GOVERNANCE_ENFORCEMENT_SOURCES.issubset(protected_sources):
        errors.append("governance enforcement sources are not integrity protected")
    for relative in protected_sources:
        source = root / relative
        if not source.is_file():
            errors.append(f"protected governance source is missing: {relative}")
            continue
        try:
            content = source.read_text(encoding="utf-8")
            if source.suffix == ".py":
                ast.parse(content, filename=relative)
            elif source.suffix == ".ts":
                if not content.strip() or "\x00" in content:
                    raise ValueError("empty or invalid TypeScript source")
            else:
                raise ValueError("unsupported protected source type")
        except (OSError, SyntaxError, UnicodeError, ValueError) as error:
            errors.append(f"protected governance source is invalid: {relative}: {error}")
        if not _is_operating_system_read_only(source):
            errors.append(f"protected governance source is not read-only: {relative}")

    for relative in FORBIDDEN_LEGACY_SOURCES:
        if (root / relative).exists():
            errors.append(f"forbidden legacy governance source exists: {relative}")

    if identity_group.group_id != directory.active_identity_group_id:
        errors.append("active identity group does not match directory authority")
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
    if (
        shared_policy.database_path
        != "postgresql:gptbridge_transport:system"
        or shared_policy.ai_database_path
        != "postgresql:gptbridge_transport:ai"
        or shared_policy.database_path == shared_policy.ai_database_path
    ):
        errors.append("shared layer channel databases are not isolated")
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

    main_source = (root / "main-system/src-core/main.py").read_text(encoding="utf-8")
    if "GovernanceEnforcer" in main_source or "governance.enforcer" in main_source:
        errors.append("main system still references the legacy governance enforcer")
    if "MainSystemGovernance.from_environment" not in main_source:
        errors.append("main system is not launcher-attested")
    if "CoreLogger" in main_source:
        errors.append("main system must not implement persistent logging")

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
    launcher_source = (root / "main-system/src-ui/main/python-backend.ts").read_text(
        encoding="utf-8"
    )
    if "createMainSystemGovernanceBootstrap" not in launcher_source:
        errors.append("shared launcher does not create governance bootstrap material")
    bootstrap_source = (
        root / "main-system/src-ui/main/governance-bootstrap.ts"
    ).read_text(encoding="utf-8")
    bootstrap_protected_sources = tuple(
        re.findall(
            r"'((?:governance_rule|main-system)/[^']+\.(?:py|ts))'",
            bootstrap_source,
        )
    )
    if bootstrap_protected_sources != protected_sources:
        errors.append(
            "main-system governance bootstrap sources do not match governance authority"
        )
    main_launcher_source = (
        root / "main-system/src-ui/main/index.ts"
    ).read_text(encoding="utf-8")
    if "preloadDefaultGovernanceAuthority" not in main_launcher_source:
        errors.append("main system does not preload governance before startup")
    runtime_source = (
        root / "main-system/src-core/core_system/governance_runtime.py"
    ).read_text(encoding="utf-8")
    if 'if tool_id == "governance_rule"' not in runtime_source:
        errors.append("governance lifecycle control is not explicitly denied")

    manifest_tool_ids: set[str] = set()
    for manifest_path in sorted(root.glob("*/manifest.json")):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            errors.append(f"invalid tool manifest: {manifest_path}: {error}")
            continue
        tool_id = str(manifest.get("id") or "")
        manifest_tool_ids.add(tool_id)
        physical_owner_root = str(manifest.get("physical_owner_root") or "")
        if (
            tool_id != manifest_path.parent.name
            and physical_owner_root != manifest_path.parent.name
        ):
            errors.append(f"tool identity mismatch: {manifest_path}")
        if re.fullmatch(label_policy.tool_id_pattern, tool_id) is None:
            errors.append(f"tool identifier is not standardized: {tool_id}")
        if "name" in manifest or manifest.get("name_key") != "tool.name":
            errors.append(f"tool name label is not standardized: {tool_id}")
        if tool_id == "governance_rule":
            expected_lifecycle = {
                "startup": "default-before-main-system",
                "directLoad": True,
                "encapsulated": False,
                "optional": False,
                "stoppable": False,
                "disableable": False,
                "unloadable": False,
            }
            if (
                manifest.get("status") != "running"
                or manifest.get("lifecycle") != expected_lifecycle
                or "executable" in manifest
            ):
                errors.append("governance manifest lifecycle is invalid")
        capabilities = manifest.get("capabilities")
        if not isinstance(capabilities, dict):
            errors.append(f"tool capabilities are missing: {tool_id}")
        else:
            for capability_name in capabilities:
                if (
                    capability_name not in code_rules.approved_capability_names
                    or re.fullmatch(
                        label_policy.capability_pattern,
                        capability_name,
                    )
                    is None
                ):
                    errors.append(
                        f"tool capability label is not standardized: "
                        f"{tool_id}:{capability_name}"
                    )
        window = manifest.get("window")
        if isinstance(window, dict) and (
            "title" in window
            or window.get("title_key") != "tool.window_title"
        ):
            errors.append(f"tool window label is not standardized: {tool_id}")
        locale_path = manifest_path.parent / "locales" / "zh-TW.json"
        try:
            locale = json.loads(locale_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            errors.append(f"traditional Chinese locale is invalid: {tool_id}: {error}")
            locale = {}
        if not isinstance(locale, dict) or not all(
            isinstance(key, str)
            and re.fullmatch(label_policy.locale_key_pattern, key)
            and isinstance(value, str)
            for key, value in locale.items()
        ):
            errors.append(f"traditional Chinese locale schema is invalid: {tool_id}")
        if not set(code_rules.required_locale_keys).issubset(locale):
            errors.append(f"traditional Chinese locale keys are incomplete: {tool_id}")
        permissions = manifest.get("permissions")
        if not isinstance(permissions, dict):
            errors.append(f"tool permissions are missing: {tool_id}")
        expected_code_scope = (
            "project-source-excluding-governance-rule"
            if tool_id == "xingcheng"
            else "tool-root-only"
        )
        expected_database_scope = (
            "opaque-central-index-read-and-xingcheng-internal-read-write"
            if tool_id == "xingcheng"
            else "tool-database-only"
            if tool_id != "governance_rule"
            else "none"
        )
        if permissions.get("code_scope") != expected_code_scope:
            errors.append(f"tool code scope is invalid: {tool_id}")
        database_scope = permissions.get("database_scope")
        if database_scope != expected_database_scope:
            errors.append(f"tool database scope is invalid: {tool_id}")
    
    # Add companion tool star-chat which is nested under xingcheng
    if (root / "local-model" / "model-dialogue" / "manifest.json").is_file():
        manifest_tool_ids.add("star-chat")

    registered_tool_ids = {
        identity.bound_tool_id
        for identity in identity_group.identities
        if identity.bound_tool_id != "main-system"
    }
    if registered_tool_ids != manifest_tool_ids:
        errors.append("each independent tool must have exactly one enabled identity")
    if manifest_tool_ids != set(code_rules.approved_tool_ids):
        errors.append("tool identifiers do not match the approved name list")

    return errors


def main() -> int:
    errors = audit_runtime_governance()
    if errors:
        for error in errors:
            print(f"[FAIL] {error}")
        return 1
    print("[PASS] single-authority runtime governance")
    return 0


__all__ = ("audit_runtime_governance", "main")
