"""Codex-mirror table driven check sections (split from export_audit_manifest)."""

from __future__ import annotations

import json
from pathlib import Path


def emit_contract_axes(ctx) -> None:
    emit = ctx.emit
    contains = ctx.contains
    not_contains = ctx.not_contains
    checks = ctx.checks
    root = ctx.root
    # --- check_contract_axes (G48) ------------------------------------
    for axis in ( "ai-connection", "backend-lifecycle", "data-architecture", "ipc", "sql-schema", "tool-runtime", ):
        rel_axis = f"main-system/config/{axis}-contract.json"
        emit(f"contract-axis:{axis}:exists", "file-exists", rel_axis)
        emit(f"contract-axis:{axis}:parse", "json-parses", rel_axis)
        try:
            _axis_doc = json.loads( (root / rel_axis).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            _axis_doc = {}
        _ver = _axis_doc.get("contract_version")
        _min = _axis_doc.get("minimum_supported_contract_version")
        if isinstance(_ver, int):
            bound = _min if isinstance(_min, int) and _min >= 1 else 1
            emit(f"contract-axis:{axis}:version", "json-key-value", rel_axis, markers=[f"contract_version>={bound}"])
        else:
            emit(f"contract-axis:{axis}:schema", "json-has-keys", rel_axis, markers=["schema"])
        if isinstance(_min, int):
            emit(f"contract-axis:{axis}:minimum", "json-key-value",
                 rel_axis,
                 markers=["minimum_supported_contract_version>=0"])



def emit_activation_states(ctx) -> None:
    _tab = ctx.tables
    _table_absent = _tab._table_absent
    _table_rows_for = _tab._table_rows_for
    _table_assert = _tab._table_assert
    _table_present = _tab._table_present
    _rows_or_count = _tab._rows_or_count
    _bind = _tab._bind
    _relativize = ctx.relativize
    _mirror = ctx.mirror
    emit = ctx.emit
    contains = ctx.contains
    not_contains = ctx.not_contains
    checks = ctx.checks
    root = ctx.root
    # --- check_activation_states --------------------------------------
    _arch_roots = {
        str(r.get("architecture_code")): str(r.get("physical_root") or "")
        for r in _table_rows_for("project_architecture_directory")
    }
    _retired_sovereigns = sorted({
        str(r.get("sovereign_id") or r.get("id") or "")
        for r in _table_rows_for("sovereigns")
        if "retired" in str(r.get("rank") or "")
    } - {""})
    _act_entry = _mirror.get("architecture_activation_states")
    if _act_entry is None:
        _table_absent("architecture_activation_states")
    else:
        act_part, act_rows = _act_entry
        for sid in _retired_sovereigns:
            emit(f"activation:retired-owner:{sid}", "file-not-contains",
                 act_part, markers=[f'"verification_owner":"{sid}"'])
        for row in act_rows:
            code = str(row.get("architecture_code") or "?")
            owner = str(row.get("verification_owner") or "")
            if owner:
                _table_assert(
                    f"activation:owner:{code}",
                    "architecture_activation_states",
                    [f"architecture_code={code}"]
                    + [f"verification_owner!={s}" for s in _retired_sovereigns])
            target = str(row.get("target_root") or "")
            if target and row.get("current_state") in ("active", "mandated"):
                if target.startswith("ARCH_CODE:"):
                    resolved = _arch_roots.get( target.removeprefix("ARCH_CODE:"), "")
                else:
                    resolved = target
                if not resolved:
                    emit(f"activation:unresolved-target:{code}", "fail",
                         reason="activation target_root code unregistered: "
                                f"{code}: {target}")
                else:
                    emit(f"activation:target:{code}", "dir-exists", _relativize(resolved))
            legacy = str(row.get("legacy_root") or "")
            if (legacy
                    and row.get("old_root_deletion_state")
                    == "not-applicable"
                    and not legacy.startswith("ARCH_LEGACY_CODE:")):
                emit(f"activation:legacy-gone:{code}", "file-not-exists", _relativize(legacy))



def emit_formal_rules(ctx) -> None:
    _tab = ctx.tables
    _table_absent = _tab._table_absent
    _table_rows_for = _tab._table_rows_for
    _table_assert = _tab._table_assert
    _table_present = _tab._table_present
    _rows_or_count = _tab._rows_or_count
    _bind = _tab._bind
    _relativize = ctx.relativize
    _mirror = ctx.mirror
    emit = ctx.emit
    contains = ctx.contains
    not_contains = ctx.not_contains
    checks = ctx.checks
    root = ctx.root
    # --- check_formal_rules -------------------------------------------
    _table_present("formal_rule_registry")
    _evaluators_src = ( "governance_rule/execution/formal_rules/evaluators.py")
    for frule in _table_rows_for("formal_rule_registry"):
        code = str(frule.get("rule_code") or "")
        if not code:
            continue
        status = str(frule.get("status") or "")
        if status in ("withdrawn", "retired"):
            continue
        contains(f"formal-rule:evaluator:{code}", _evaluators_src, [f'"{code}"'])
        pid = str(frule.get("controlling_provision_id") or "")
        _table_assert(
            f"formal-rule:row:{code}", "formal_rule_registry",
            [f"rule_code={code}", f"controlling_provision_id={pid}", f"status={status}"])
        if status == "active":
            _table_assert(
                f"formal-rule:parity:{code}", "formal_rule_registry",
                [f"rule_code={code}", "parity_status=VERIFIED"])



def emit_implementation_obligations(ctx) -> None:
    _tab = ctx.tables
    _table_absent = _tab._table_absent
    _table_rows_for = _tab._table_rows_for
    _table_assert = _tab._table_assert
    _table_present = _tab._table_present
    _rows_or_count = _tab._rows_or_count
    _bind = _tab._bind
    _relativize = ctx.relativize
    _mirror = ctx.mirror
    emit = ctx.emit
    contains = ctx.contains
    not_contains = ctx.not_contains
    checks = ctx.checks
    root = ctx.root
    # --- check_implementation_obligations (A292) ----------------------
    _table_present("implementation_obligations")
    for oblig in _table_rows_for("implementation_obligations"):
        ocode = str(oblig.get("obligation_code") or "")
        if not ocode:
            continue
        _table_assert(
            f"obligation:row:{ocode}", "implementation_obligations",
            [f"obligation_code={ocode}"]
            + _bind("implementation_owner", oblig.get("implementation_owner"))
            + _bind("target_state", oblig.get("target_state"))
            + _bind("acceptance_evidence", oblig.get("acceptance_evidence"))
            + _bind("current_state", oblig.get("current_state")))
    for rcode in (
        "OBL_LAYERED_ARCHITECTURE", "OBL_ARCHITECTURE_CATALOG",
        "OBL_TEST_SUITE_DIRECTORY", "OBL_TOP_LEVEL_PATHS",
        "OBL_ANTI_JAILBREAK", "OBL_SYSTEM_RELIABILITY",
        "OBL_星澄_AUDIT", "OBL_CHANNEL_ANOMALY_ISOLATION",
        "OBL_NATIVE_PROMOTION_RECORD_CHECKER",
        "OBL_LANGUAGE_DEPENDENCY_DAG_GATE",
        "OBL_DIRECTORY_GOVERNANCE_DATA_CLOSURE",
        "OBL_FORMAL_EVALUATOR_V2_PARITY",
    ):
        _table_assert( f"obligation:required:{rcode}", "implementation_obligations", [f"obligation_code={rcode}"])



def emit_codex_consistency(ctx) -> None:
    _tab = ctx.tables
    _table_absent = _tab._table_absent
    _table_rows_for = _tab._table_rows_for
    _table_assert = _tab._table_assert
    _table_present = _tab._table_present
    _rows_or_count = _tab._rows_or_count
    _bind = _tab._bind
    _relativize = ctx.relativize
    _mirror = ctx.mirror
    emit = ctx.emit
    contains = ctx.contains
    not_contains = ctx.not_contains
    checks = ctx.checks
    root = ctx.root
    # --- check_codex_consistency semantic half -------------------------
    _p1 = "governance_rule/codex/governance_codex.zh-TW.part-1.txt"
    # Mirror carries the authoritative version+hash; the release pin is
    # release-cadence and may lag — bind stable fields, not equality.
    emit("codex-consistency:pin-keys", "json-has-keys",
         "shared-layer/release-dependencies.json",
         markers=["governance_references"])
    emit("codex-consistency:pin-values", "json-key-value",
         "shared-layer/release-dependencies.json",
         markers=[
             "governance_references.codex_identity"
             "=governance-codex://official",
             "governance_references.codex_authority"
             "=postgresql://local/gptbridge_codex",
             "governance_references.codex_version^=20"])
    emit("codex-consistency:mirror-keys", "json-has-keys", _p1,
         markers=["codex_version", "assembled_payload_hash", "part_hash", "mirror_id"])
    for required_table in (
        "metadata", "revision_history", "seal_manifest",
        "certification_policy", "version_evolution_rules",
        "provision_identities", "provision_lineage",
    ):
        _table_present(required_table)
    for tbl, key in (("sovereigns", "sovereign_id"),
                     ("principles", "provision_id"),
                     ("articles", "provision_id"),
                     ("edicts", "provision_id")):
        _rows_or_count(f"codex-id:{tbl}", tbl, key)



def emit_directory_audit(ctx) -> None:
    _tab = ctx.tables
    _table_absent = _tab._table_absent
    _table_rows_for = _tab._table_rows_for
    _table_assert = _tab._table_assert
    _table_present = _tab._table_present
    _rows_or_count = _tab._rows_or_count
    _bind = _tab._bind
    _relativize = ctx.relativize
    _mirror = ctx.mirror
    emit = ctx.emit
    contains = ctx.contains
    not_contains = ctx.not_contains
    checks = ctx.checks
    root = ctx.root
    # --- check_directory_audit family ----------------------------------
    from governance_rule.execution.audit.audit_directories import (
        DIRECTORY_OWNERS as _DIR_OWNERS,
        DIRECTORY_TABLES as _DIR_TABLES,
        REQUIRED_SCHEMA_TABLES as _REQ_TABLES,
    )
    for table in sorted(_REQ_TABLES):
        _table_present(table)
    _catalog = _table_rows_for("directory_master_catalog")
    _contract_codes = {
        str(r.get("directory_code"))
        for r in _table_rows_for("directory_format_contract")
    }
    _catalog_codes = {
        str(r.get("directory_code")) for r in _catalog}
    for ccode in sorted(_contract_codes | _catalog_codes):
        _table_assert(f"catalog:contract-has:{ccode}", "directory_master_catalog", [f"directory_code={ccode}"])
        _table_assert(f"catalog:catalog-has:{ccode}", "directory_format_contract", [f"directory_code={ccode}"])
    for crow in _catalog:
        ccode = str(crow.get("directory_code") or "")
        expected_owner = (
            "learning-evidence-sync-sub-sovereign"
            if ccode == "DIR_MAINTENANCE_MANUAL"
            else "permission-sovereign")
        _table_assert(
            f"catalog:owner:{ccode}", "directory_master_catalog",
            [f"directory_code={ccode}", f"owner={expected_owner}"])
        physical = str(crow.get("canonical_name") or "").replace( "-", "_")
        if crow.get("implementation_state") == "active" and physical:
            _table_present(physical)
    # Per-directory-row bindings (identity value + owner + versions —
    for table, idcol in _DIR_TABLES.items():
        _dir_rows = _table_rows_for(table)
        for drow in _dir_rows:
            if drow.get(idcol) is None:
                emit(f"directory:identity-missing:{table}", "fail", reason=f"{table} row missing {idcol}")
        _rows_or_count(f"directory:identity:{table}", table, idcol,
                       extra=lambda r, _t=table: _bind( "owner", _DIR_OWNERS[_t]))
    _class_rows = _table_rows_for("provision_law_classification")
    if _class_rows:
        _table_assert( "provision-class:count", "provision_law_classification", [], count=len(_class_rows))
        for law in sorted({ str(r.get("law_code")) for r in _class_rows if r.get("law_code")}):
            _table_assert(f"provision-law:{law}", "law_structure_directory", [f"law_code={law}"])
    # Seal completeness on the current seal row.
    _seals = _table_rows_for("seal_manifest")
    if not _seals:
        emit("directory:seal-empty", "fail", reason="seal manifest is empty")
    else:
        _current_seal = max( _seals, key=lambda r: str(r.get("version") or ""))
        _sv = str(_current_seal.get("version") or "")
        _table_assert(
            f"directory:seal:{_sv}", "seal_manifest",
            [f"version={_sv}"]
            + _bind("certification_state", _current_seal.get("certification_state"))
            + _bind("content_root", _current_seal.get("content_root"))
            + _bind("identity_root", _current_seal.get("identity_root"))
            + _bind("full_root", _current_seal.get("full_root"))
            + _bind("history_head", _current_seal.get("history_head")))
