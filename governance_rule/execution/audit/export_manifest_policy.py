"""Authority/identity policy check sections (split from export_audit_manifest)."""

from __future__ import annotations


def emit_authority_policies(ctx) -> None:
    emit = ctx.emit
    contains = ctx.contains
    not_contains = ctx.not_contains
    checks = ctx.checks
    root = ctx.root
    _policy = ctx.policy
    _directory = ctx.directory
    _code_rules = ctx.code_rules
    # --- authority policy family (registry literal bindings) ----------
    _policy_src = "governance_rule/governance_policy.py"
    _dir_src = ("governance_rule/permission_directory/" "directory_authority.py")
    _code_src = "governance_rule/code_rule_directory.py"

    contains("authority-policy:policy", _policy_src, [
        f'authority="{_policy.authority}"',
        f'top_level_rule="{_policy.top_level_rule}"',
        f"governance_rule_count={_policy.governance_rule_count}",
        "governance_rule_partitioning=False",
        "subordinate_governance_rule_definition=False",
        f'permission_hierarchy_role="'
        f'{_policy.permission_hierarchy_role}"',
    ])
    contains("authority-policy:directory", _dir_src, [
        'permission_hierarchy_role='
        '"subordinate-read-only-permission-directory"',
        f"current_version={_policy.authority_version}",
    ])
    contains("authority-policy:code-rules", _code_src, [
        '"codex-v1.32010-is-sole-rule-source"',
        f'governing_source="{_policy.governance_rule_sources[0]}"',
        "independent_authority=False",
        "runtime_write_allowed=False",
        f'canonical_project_root="'
        f'{_policy.code_architecture.all_source_code_root}"',
    ])
    _resp = _policy.system_responsibilities
    contains("authority-policy:responsibilities", _policy_src, [
        f'git="{_resp.git}"',
        f'sql="{_resp.sql}"',
        f'vector_rag="{_resp.vector_rag}"',
        f'local_vector_fallback="{_resp.local_vector_fallback}"',
        f'llm="{_resp.llm}"',
        f'separation="{_resp.separation}"',
        f'governed_flow="{_resp.governed_flow}"',
        f'management_owner="{_resp.management_owner}"',
        "llm_inference_as_source_of_truth=False",
    ])



def emit_shared_layer_policies(ctx) -> None:
    emit = ctx.emit
    contains = ctx.contains
    not_contains = ctx.not_contains
    checks = ctx.checks
    root = ctx.root
    _policy = ctx.policy
    _directory = ctx.directory
    _code_rules = ctx.code_rules
    _policy_src = "governance_rule/governance_policy.py"
    _dir_src = ("governance_rule/permission_directory/" "directory_authority.py")
    _code_src = "governance_rule/code_rule_directory.py"
    _shared = _directory.shared_layer_access_policy
    _activation = _policy.activation
    contains("shared-layer-policy:directory", _dir_src, [
        f'module_root="{_shared.module_root}"',
        'jurisdiction="governance-policy-only"',
        "main_system_module_member=False",
        "token_required=True", "database_only=True",
        "source_write=False", "direct_data_write=False",
        "executable_content=False", "direct_process_instruction=False",
        'unchanneled_instruction="PERMISSION_DENIED"',
        f'database_path="{_shared.database_path}"',
        f'ai_database_path="{_shared.ai_database_path}"',
    ])
    contains("shared-layer-policy:activation", _policy_src, [
        "default_active=True",
        f'activation_order="{_activation.activation_order}"',
        "direct_load_required=True",
        f'independent_tool_packaging_exception="'
        f'{_activation.independent_tool_packaging_exception}"',
        "packaged_executable_allowed=False",
        f'execution_access="{_activation.execution_access}"',
        "encapsulation_allowed=False",
        "optional=False", "stop_permission=False",
        "disable_permission=False", "unload_permission=False",
        f'lifetime="{_activation.lifetime}"',
        f'main_system_start_failure="'
        f'{_activation.main_system_start_failure}"',
        'main_system_repair_authority="governance-policy-only"',
        f'main_system_repair_scope="'
        f'{_activation.main_system_repair_scope}"',
        'repair_completion_gate="governance-reverify-before-normal-mode"',
    ])
    contains("shared-layer-policy:directory-lifecycle", _dir_src, [
        "governance_default_active=True",
        f'governance_activation_order="{_activation.activation_order}"',
        "governance_direct_load=True",
        "governance_packaged_executable_allowed=False",
        f'governance_execution_access="{_activation.execution_access}"',
        "governance_encapsulation_allowed=False",
        "governance_stop_permission=False",
        "governance_disable_permission=False",
        "governance_unload_permission=False",
    ])
    contains("shared-layer-policy:labels", _policy_src, [ "aliases_allowed=False", "category_labels_allowed=False", ])
    not_contains("shared-layer-policy:no-categories", _code_src, ["category_labels=True"])

    _repair = _policy.automatic_repair
    contains("repair-policy:policy", _policy_src, [
        "backup_assistance_allowed=True",
        f'backup_owner="{_repair.backup_owner}"',
        "direct_backup_access=False",
        f'backup_request_channel="{_repair.backup_request_channel}"',
        "authorization_per_step=True",
        "authority_restore_from_backup=False",
    ])
    contains("repair-policy:boundaries",
             "governance_rule/permission_directory/registries/"
             "permissions/capability_boundaries.py",
             ["automatic-repair"])


def emit_identity_bindings(ctx) -> None:
    emit = ctx.emit
    contains = ctx.contains
    not_contains = ctx.not_contains
    checks = ctx.checks
    root = ctx.root
    _literal_assert = ctx.literal_assert
    _identity_group = ctx.identity_group
    _bindings = ctx.bindings
    _cap_bounds = ctx.cap_bounds
    _code_rules = ctx.code_rules
    _code_src = "governance_rule/code_rule_directory.py"
    _ig_src = ("governance_rule/permission_directory/registries/" "permissions/identity_groups.py")
    _ip_src = ("governance_rule/permission_directory/registries/" "permissions/identity_permissions.py")
    _cb_src = ("governance_rule/permission_directory/registries/" "permissions/capability_boundaries.py")
    _registry_dir = ( root / "governance_rule" / "permission_directory" / "registries")
    _registry_files = [ p for p in sorted(_registry_dir.rglob("*.py")) if "__pycache__" not in p.parts]
    for ident in _identity_group.identities:
        _literal_assert(f"identity:actor:{ident.actor}", f'actor="{ident.actor}"')
        _literal_assert(f"identity:code:{ident.actor}", f'identity_code="{ident.identity_code}"')
        if ident.bound_tool_id:
            _literal_assert(f"identity:tool:{ident.actor}", f'"{ident.bound_tool_id}"')
        if ident.lifecycle != "active":
            _literal_assert(f"identity:lifecycle:{ident.actor}", f'lifecycle="{ident.lifecycle}"')
    for binding in _bindings:
        _literal_assert(f"binding:actor:{binding.actor}", f'actor="{binding.actor}"')
        for cap in binding.capabilities:
            _literal_assert(f"binding:cap:{binding.actor}:{cap}", f'"{cap}"')
    for cap_item in _cap_bounds:
        contains(f"capability:{cap_item.capability}", _cb_src, [f'"{cap_item.capability}"'])
    not_contains("identity:no-arbitrary-storage", _ip_src, ['"shared-layer-read-write"'])
    not_contains("capability:no-arbitrary-storage", _cb_src, ['"shared-layer-read-write"'])
    for approved in _code_rules.approved_actor_names:
        contains(f"approved-actor:{approved}", _code_src, [f'"{approved}"'])
    for approved_tool in _code_rules.approved_tool_ids:
        contains(f"approved-tool:{approved_tool}", _code_src, [f'"{approved_tool}"'])

    _NON_INDEPENDENT = frozenset({ "governance_rule", "shared-layer", "star-chat", "xingcheng-assistant"})
    _retired_ids = {
        i.bound_tool_id for i in _identity_group.identities
        if i.lifecycle == "retired"}
    for ident in _identity_group.identities:
        tid = ident.bound_tool_id
        if (tid in ("main-system",) or tid in _NON_INDEPENDENT or tid in _retired_ids):
            continue
        _literal_assert(f"tool-identity:{tid}", f'actor="governance/tool/{tid}"')
        contains(f"tool-approved:{tid}", _code_src, [f'"{tid}"'])



def emit_git_tiers_remainder(ctx) -> None:
    emit = ctx.emit
    contains = ctx.contains
    not_contains = ctx.not_contains
    checks = ctx.checks
    root = ctx.root
    # --- check_git_tiers classify-failclosed remainder -----------------
    contains("git-tiers:classify-failclosed",
             "governance_rule/execution/git_tiers/__init__.py",
             ["def classify", "TIER1_OPS", "TIER2_OPS", "TIER3_OPS", "return 3"])
    contains("git-tiers:pre-push-gate", "governance_rule/git-hooks/pre-push",
             ["GOVERNANCE_AUTHORITY_APPROVAL", "merge-base", "refs/tags/"])
    for hook in ("pre-commit", "pre-merge-commit", "pre-push"):
        emit(f"git-tiers:hook:{hook}", "file-exists", f"governance_rule/git-hooks/{hook}")
