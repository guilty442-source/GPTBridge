from __future__ import annotations

import ast
import json
import re
import stat
import subprocess
import sys
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
from governance_rule.codex.chinese import GOVERNANCE_CODEX_CHINESE
import governance_rule.execution.git_tiers
governance_rule.execution.git_tiers.AUDIT_LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
governance_rule.execution.git_tiers.AUDIT_LEDGER_PATH.touch(exist_ok=True)


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
        "governance_rule/execution/git_tiers/__init__.py",
        "governance_rule/permission_directory/execution/identity_registry/__init__.py",
        "governance_rule/permission_directory/execution/path_guard/__init__.py",
        "main-system/src-ui/main/governance-bootstrap.ts",
        "main-system/src-core/core_system/governance_runtime.py",
    }
)

SELF_HEALTH_MANAGED_TEST_FILES = frozenset(
    {
        # ── main-system (central runtime + repair authority) ──────────
        "main-system/tests/test_main_system.py",
        # ── governance_rule (codex + enforcement) ─────────────────────
        "governance_rule/tests/test_governance_health.py",
        # ── shared-layer (central SQL index + channel) ────────────────
        "shared-layer/tests/test_shared_layer.py",
        # ── local-model / xingcheng (native model platform) ──────────
        "local-model/tests/test_xingcheng.py",
        # ── local-model / model-dialogue / star-chat ─────────────────
        "local-model/model-dialogue/tests/test_star_chat.py",
        # ── global-cleaner (backup + cleanup infrastructure) ─────────
        "global-cleaner/tests/test_global_cleaner.py",
        # ── ai-collaboration (governed browser automation) ───────────
        "ai-collaboration/tests/test_ai_collaboration.py",
        # ── ai-assistant (investment + assistant UI) ─────────────────
        "ai-assistant/tests/test_ai_assistant.py",
        # ── vaultly (encryption + vault) ─────────────────────────────
        "vaultly/tests/test_vaultly.py",
        # ── file-sorter (governed file sorting) ──────────────────────
        "file-sorter/tests/test_file_sorter.py",
        # ── system-rescue (central repair + packaging) ───────────────
        "system-rescue/tests/test_system_rescue.py",
        # ── investment-mobile (mobile channel) ───────────────────────
        "investment-mobile/tests/test_investment_mobile.py",
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

    if GOVERNANCE_CODEX.codex_version < 3:
        errors.append("governance codex version must include the unified architecture policy")
    if policy.authority != "governance-codex-v3-derived-enforcement-policy":
        errors.append("governance policy must remain the codex-v3-derived enforcement projection")
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
    if "codex-v3-is-sole-rule-source" not in code_rules.requirements:
        errors.append("code rule directory does not declare the codex v3 authority source")
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
    physical_owner_roots: set[str] = set()

    # Enforce nested_independent_tool_folder=False: no manifests at depth 3+
    deep_manifests = sorted(root.glob("*/*/*/manifest.json"))
    if deep_manifests:
        errors.append(
            "nested independent tool folder exceeds allowed depth: "
            f"{[str(p.relative_to(root)) for p in deep_manifests]}"
        )

    # Pass 1: depth-1 manifests (direct children of project root)
    for manifest_path in sorted(root.glob("*/manifest.json")):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            errors.append(f"invalid tool manifest: {manifest_path}: {error}")
            continue
        tool_id = str(manifest.get("id") or "")
        manifest_tool_ids.add(tool_id)
        physical_owner_root = str(manifest.get("physical_owner_root") or "")
        if physical_owner_root:
            physical_owner_roots.add(physical_owner_root)
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

    # Pass 2: depth-2 manifests (companion tools nested under a
    # physical_owner_root).  These tools share their owner's permission
    # profile, so the standard code_scope/database_scope checks are not
    # applied — only identity, locale, capability and window checks.
    for manifest_path in sorted(root.glob("*/*/manifest.json")):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            errors.append(f"invalid tool manifest: {manifest_path}: {error}")
            continue
        tool_id = str(manifest.get("id") or "")
        manifest_tool_ids.add(tool_id)
        physical_owner_root = str(manifest.get("physical_owner_root") or "")
        grandparent_name = manifest_path.parent.parent.name

        # Enforce independent_tool_direct_child_only=True: a nested manifest
        # must declare a physical_owner_root that matches its grandparent
        # directory, and that grandparent must be a known physical owner root.
        if not physical_owner_root:
            errors.append(
                f"nested tool manifest lacks physical_owner_root: {manifest_path}"
            )
        elif physical_owner_root != grandparent_name:
            errors.append(
                f"nested tool physical_owner_root does not match parent root: "
                f"{manifest_path}"
            )
        elif physical_owner_root not in physical_owner_roots:
            errors.append(
                f"nested tool references unknown physical_owner_root: "
                f"{manifest_path}"
            )

        if re.fullmatch(label_policy.tool_id_pattern, tool_id) is None:
            errors.append(f"tool identifier is not standardized: {tool_id}")
        if "name" in manifest or manifest.get("name_key") != "tool.name":
            errors.append(f"tool name label is not standardized: {tool_id}")
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

    registered_tool_ids = {
        identity.bound_tool_id
        for identity in identity_group.identities
        if identity.bound_tool_id != "main-system"
    }
    if registered_tool_ids != manifest_tool_ids:
        errors.append("each independent tool must have exactly one enabled identity")
    if manifest_tool_ids != set(code_rules.approved_tool_ids):
        errors.append("tool identifiers do not match the approved name list")

    # Codex consistency: the Chinese backup reference must contain every
    # principle, article, edict and sovereign declared in the authoritative
    # codex (A36/E22/P17 — Chinese codex is backup-only but must stay complete).
    auth_principle_ids = {p.id for p in GOVERNANCE_CODEX.principles}
    chinese_principle_ids = {p.id for p in GOVERNANCE_CODEX_CHINESE.principles}
    if auth_principle_ids != chinese_principle_ids:
        missing = sorted(auth_principle_ids - chinese_principle_ids)
        extra = sorted(chinese_principle_ids - auth_principle_ids)
        if missing:
            errors.append(f"Chinese codex is missing principles: {missing}")
        if extra:
            errors.append(f"Chinese codex has extra principles: {extra}")

    auth_article_ids = {a.id for a in GOVERNANCE_CODEX.articles}
    chinese_article_ids = {a.id for a in GOVERNANCE_CODEX_CHINESE.articles}
    if auth_article_ids != chinese_article_ids:
        missing = sorted(auth_article_ids - chinese_article_ids)
        extra = sorted(chinese_article_ids - auth_article_ids)
        if missing:
            errors.append(f"Chinese codex is missing articles: {missing}")
        if extra:
            errors.append(f"Chinese codex has extra articles: {extra}")

    auth_edict_ids = {e.id for e in GOVERNANCE_CODEX.edicts}
    chinese_edict_ids = {e.id for e in GOVERNANCE_CODEX_CHINESE.edicts}
    if auth_edict_ids != chinese_edict_ids:
        missing = sorted(auth_edict_ids - chinese_edict_ids)
        extra = sorted(chinese_edict_ids - auth_edict_ids)
        if missing:
            errors.append(f"Chinese codex is missing edicts: {missing}")
        if extra:
            errors.append(f"Chinese codex has extra edicts: {extra}")

    auth_sovereign_ids = {s.id for s in GOVERNANCE_CODEX.sovereigns}
    chinese_sovereign_ids = {s.id for s in GOVERNANCE_CODEX_CHINESE.sovereigns}
    if auth_sovereign_ids != chinese_sovereign_ids:
        missing = sorted(auth_sovereign_ids - chinese_sovereign_ids)
        extra = sorted(chinese_sovereign_ids - auth_sovereign_ids)
        if missing:
            errors.append(f"Chinese codex is missing sovereigns: {missing}")
        if extra:
            errors.append(f"Chinese codex has extra sovereigns: {extra}")

    # Git tier enforcement (A53/E39): verify the three enforcement layers
    # exist and contain the required governance logic.
    git_tiers_source = root / "governance_rule" / "execution" / "git_tiers" / "__init__.py"
    if not git_tiers_source.is_file():
        errors.append("git tier enforcement module is missing")
    else:
        git_tiers_text = git_tiers_source.read_text(encoding="utf-8")
        if "TIER1_OPS" not in git_tiers_text or "TIER2_OPS" not in git_tiers_text or "TIER3_OPS" not in git_tiers_text:
            errors.append("git tier module is missing tier operation sets")
        if "def classify" not in git_tiers_text or "def enforce" not in git_tiers_text:
            errors.append("git tier module is missing classify/enforce functions")
        if "def audit_log" not in git_tiers_text:
            errors.append("git tier module is missing audit_log function")
        if governance_rule.execution.git_tiers.classify("unknown-governance-operation") != 3:
            errors.append("unknown git operations must fail closed as tier 3")

    git_gate_source = root / "scripts" / "git-gate.py"
    if not git_gate_source.is_file():
        errors.append("git gate wrapper is missing")
    else:
        git_gate_text = git_gate_source.read_text(encoding="utf-8")
        if "from governance_rule.execution.git_tiers import" not in git_gate_text:
            errors.append("git gate wrapper does not import git_tiers module")

    hook_root = root / "governance_rule" / "git-hooks"
    for hook_name in ("pre-commit", "pre-merge-commit", "pre-push"):
        hook_source = hook_root / hook_name
        if not hook_source.is_file():
            errors.append(f"governed Git hook is missing: {hook_name}")
    pre_push_source = hook_root / "pre-push"
    if pre_push_source.is_file():
        hook_text = pre_push_source.read_text(encoding="utf-8")
        if "GOVERNANCE_AUTHORITY_APPROVAL" not in hook_text:
            errors.append("pre-push hook does not enforce governance authority approval")
        if "merge-base" not in hook_text or "refs/tags/" not in hook_text:
            errors.append("pre-push hook does not detect non-fast-forward or tag rewrites")

    # Metadata contract (A8/E21): verify the canonical metadata contract module
    # exists and exports the required fixed field names.
    metadata_contract = root / "shared-layer" / "src" / "shared_layer" / "metadata_contract.py"
    if not metadata_contract.is_file():
        errors.append("metadata contract module is missing")
    else:
        contract_text = metadata_contract.read_text(encoding="utf-8")
        for required in ("FIELD_MODULE_ID", "FIELD_RESOURCE_ID", "FIELD_LOCATOR_ID",
                         "FIELD_VERSION", "FIELD_CONTENT_HASH", "FIELD_UPDATED_AT",
                         "FIELD_STATUS", "ResourceMetadata", "validate_qdrant_payload"):
            if required not in contract_text:
                errors.append(f"metadata contract is missing: {required}")

    # Data ownership contract document (A8/E21 + A44/E30)
    ownership_doc = root / "shared-layer" / "docs" / "DATA_OWNERSHIP_CONTRACT.md"
    if not ownership_doc.is_file():
        errors.append("data ownership contract document is missing")

    # Reconcile service (A44/E30): one-directional SQLite→PostgreSQL flow
    reconcile_module = root / "shared-layer" / "src" / "shared_layer" / "reconcile.py"
    if not reconcile_module.is_file():
        errors.append("reconcile service module is missing")
    else:
        reconcile_text = reconcile_module.read_text(encoding="utf-8")
        if "ReconcileService" not in reconcile_text:
            errors.append("reconcile module is missing ReconcileService class")
        if "reconcile_module" not in reconcile_text:
            errors.append("reconcile module is missing reconcile_module method")

    # SQL migrations: verify new migration files exist (A8/E21)
    migrations_dir = root / "shared-layer" / "migrations"
    for migration_name in (
        "004_global_and_module_version_tables.sql",
        "005_central_index_composite_indexes.sql",
        "006_audit_append_only_enforcement.sql",
        "007_transport_idempotency_key.sql",
        "008_rls_role_isolation.sql",
    ):
        if not (migrations_dir / migration_name).is_file():
            errors.append(f"SQL migration is missing: {migration_name}")

    # SQLite module template (A44/E30): unified schema for all 70+ SQLite DBs
    sqlite_template = root / "shared-layer" / "sql" / "sqlite_module_template.sql"
    if not sqlite_template.is_file():
        errors.append("SQLite module template is missing")
    else:
        template_text = sqlite_template.read_text(encoding="utf-8")
        for required_table in ("schema_version", "module_metadata",
                               "resource_metadata", "audit_event", "reconcile_state"):
            if required_table not in template_text:
                errors.append(f"SQLite module template is missing table: {required_table}")

    # Embedded browser enforcement (A44/E30 + A49/E35):
    # ai-collaboration and vaultly must NOT use Playwright or external browsers.
    for module_path in (
        "ai-collaboration/src/backend/services/ai_collaboration/integration/browser_automation.py",
        "ai-collaboration/src/backend/services/ai_collaboration/integration/provider_session.py",
        "vaultly/src/backend/services/vaultly/integration/browser_session.py",
    ):
        full_path = root / module_path
        if full_path.is_file():
            content = full_path.read_text(encoding="utf-8")
            if "from playwright" in content or "import playwright" in content:
                errors.append(f"module still uses Playwright: {module_path}")
            if "async_playwright" in content and "InProcessEmbeddedBrowser" not in content:
                errors.append(f"module still uses async_playwright: {module_path}")

    # requirements.txt must not contain playwright
    requirements = root / "main-system" / "requirements.txt"
    if requirements.is_file():
        req_text = requirements.read_text(encoding="utf-8")
        if "playwright" in req_text.lower():
            errors.append("main-system/requirements.txt still depends on playwright")

    # pyproject.toml must not contain playwright
    pyproject = root / "main-system" / "pyproject.toml"
    if pyproject.is_file():
        py_text = pyproject.read_text(encoding="utf-8")
        if "playwright" in py_text.lower():
            errors.append("main-system/pyproject.toml still depends on playwright")

    # Embedded browser module must exist
    embedded_browser = root / "main-system" / "src-ui" / "main" / "embedded-browser.ts"
    if not embedded_browser.is_file():
        errors.append("embedded browser module is missing")

    # Embedded browser client must exist
    browser_client = root / "shared-layer" / "src" / "shared_layer" / "embedded_browser_client.py"
    if not browser_client.is_file():
        errors.append("embedded browser client module is missing")

    _verify_self_health_test_files(root, errors)

    return errors


def _declared_self_health_test_files(
    root: Path,
    errors: list[str],
) -> frozenset[str]:
    declared_files = {"main-system/tests/test_main_system.py"}
    manifest_paths = [
        *root.glob("*/manifest.json"),
        *root.glob("*/*/manifest.json"),
    ]
    for manifest_path in sorted(set(manifest_paths)):
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(
                f"self-health manifest is unreadable: "
                f"{manifest_path.relative_to(root).as_posix()}: {exc}"
            )
            continue
        tool_id = str(manifest.get("id") or "").strip()
        targets = manifest.get("test_targets")
        if not tool_id or not isinstance(targets, list) or not targets:
            errors.append(
                "governed tool must declare test_targets: "
                f"{manifest_path.relative_to(root).as_posix()}"
            )
            continue
        tool_root = manifest_path.parent.resolve()
        for raw_target in targets:
            relative_target = Path(str(raw_target or "").strip())
            candidate = (tool_root / relative_target).resolve()
            try:
                candidate.relative_to(tool_root)
                relative_path = candidate.relative_to(root).as_posix()
            except ValueError:
                errors.append(f"self-health test target escaped tool root: {tool_id}")
                continue
            if (
                relative_target.is_absolute()
                or candidate.suffix.casefold() != ".py"
                or not candidate.name.startswith("test_")
            ):
                errors.append(
                    f"invalid self-health test target: {tool_id}: {raw_target}"
                )
                continue
            declared_files.add(relative_path)
    return frozenset(declared_files)


def _verify_self_health_test_files(
    root: Path,
    errors: list[str],
) -> None:
    """Verify governed test files exist and can be collected by pytest.

    Maintained by the maintenance sovereign as the self-detection health
    barrier (article A57/edict E43): every governed tool keeps a test file
    that can be collected offline so governance health checks never depend
    on a live model server.
    """

    venv_python = root / "main-system" / ".venv" / "Scripts" / "python.exe"
    python_executable = str(venv_python) if venv_python.is_file() else sys.executable
    for relative_path in sorted(_declared_self_health_test_files(root, errors)):
        test_path = root / relative_path
        if not test_path.is_file():
            errors.append(f"self-health test file is missing: {relative_path}")
            continue
        try:
            completed = subprocess.run(
                [
                    python_executable,
                    "-m",
                    "pytest",
                    str(test_path),
                    "--collect-only",
                    "-q",
                    "-p",
                    "no:cacheprovider",
                ],
                cwd=str(root),
                capture_output=True,
                text=True,
                timeout=60,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired:
            errors.append(f"self-health test collection timed out: {relative_path}")
            continue
        collected = _collected_test_count(completed.stdout)
        if completed.returncode != 0:
            detail = completed.stdout.strip().splitlines()[-1:] or [
                completed.stderr.strip().splitlines()[-1:]
            ]
            errors.append(
                f"self-health test collection failed: {relative_path}: {detail}"
            )
        elif collected == 0:
            errors.append(f"self-health test file collects no tests: {relative_path}")


def _collected_test_count(output: str) -> int:
    match = re.search(r"(\d+) tests? collected", output)
    return int(match.group(1)) if match else 0


def main() -> int:
    errors = audit_runtime_governance()
    if errors:
        for error in errors:
            print(f"[FAIL] {error}")
        return 1
    print("[PASS] single-authority runtime governance")
    return 0


__all__ = ("audit_runtime_governance", "main")
