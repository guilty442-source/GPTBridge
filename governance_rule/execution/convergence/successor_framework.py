"""Codex convergence framework (framework-first; no live-codex writes).

Provides the governed successor pipeline skeleton the remaining convergence
work plugs into:

* ``stage_copy`` — isolated copy of the live codex (staging only).
* ``apply_re_tiering`` — execute a re-tiering plan (special-law migration,
  obligation delegation, retain-with-planned-successor records).
* ``apply_formal_rule_disposition`` / ``apply_obligations`` / ``apply_closures``
  — reusable successor operations.
* ``validate_staged`` — integrity + mirror-render validation of a staged copy.
* ``publish`` — guarded copy of a validated staged generation onto the live
  path (requires explicit ``approve=True``; never invoked by this framework
  automatically).
* ``version_axis_report`` / ``projection_status`` — currentness reporting for
  the version axes and derived projections.
* ``compute_closures`` — closure states from recorded evidence.

Fail-closed: any missing precondition raises ``ConvergenceError``.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

PROJECT_ROOT = Path(__file__).resolve().parents[3]
CODEX_ROOT = PROJECT_ROOT / "governance_rule" / "codex"
LIVE_CODEX = CODEX_ROOT / "data" / "governance_codex.sqlite3"
CONVERGENCE_DIR = PROJECT_ROOT / "governance_rule" / "execution" / "audit" / "convergence"

MIGRATION_STATUSES = ("retain", "special-governs", "superseded")


class ConvergenceError(RuntimeError):
    """Fail-closed convergence pipeline denial."""


@dataclass(frozen=True)
class StagedGeneration:
    path: Path
    source: Path
    created_by: str = "codex-convergence-framework"

    def connect(self) -> sqlite3.Connection:
        return sqlite3.connect(str(self.path))


@dataclass(frozen=True)
class OperationResult:
    operation: str
    applied: int = 0
    details: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"operation": self.operation, "applied": self.applied, "details": dict(self.details)}


def stage_copy(live: Path = LIVE_CODEX, *, staging_root: Path | None = None) -> StagedGeneration:
    """Copy the live codex into an isolated staging generation."""
    if not Path(live).is_file():
        raise ConvergenceError(f"CODEX_MISSING:{live}")
    root = Path(staging_root) if staging_root else Path(tempfile.mkdtemp(prefix="codex-convergence-"))
    root.mkdir(parents=True, exist_ok=True)
    target = root / "staged-codex.sqlite3"
    shutil.copy2(live, target)
    try:
        import os
        import stat as stat_module

        os.chmod(target, stat_module.S_IWRITE | stat_module.S_IREAD)
    except OSError:
        pass
    return StagedGeneration(path=target, source=Path(live))


def load_re_tiering_plan(path: Path | None = None) -> dict[str, Any]:
    plan_path = Path(path) if path else CONVERGENCE_DIR / "re_tiering_plan.json"
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ConvergenceError(f"RE_TIERING_PLAN_UNREADABLE:{error}") from error
    if not isinstance(plan.get("entries"), list) or not plan["entries"]:
        raise ConvergenceError("RE_TIERING_PLAN_EMPTY")
    return plan


def _migration(conn: sqlite3.Connection, provision_id: str, law: str, status: str,
               successor: str | None, normalization: str, version: str) -> None:
    if status not in MIGRATION_STATUSES:
        raise ConvergenceError(f"MIGRATION_STATUS_INVALID:{status}")
    conn.execute(
        "insert or replace into provision_special_law_migration "
        "(provision_type, provision_id, controlling_law_code, migration_status, successor_provision_id, "
        "normalization_status, owner, introduced_version) values (?,?,?,?,?,?,?,?)",
        ("article", provision_id, law, status, successor, normalization, "permission-sovereign", version),
    )


def _ensure_special_law(conn: sqlite3.Connection, law: str, spec: Mapping[str, Any], version: str) -> None:
    """Create the law directory rows when the target law does not exist yet."""
    if conn.execute("select 1 from law_structure_directory where law_code=?", (law,)).fetchone():
        return
    canonical = str(spec.get("canonical_name") or law.lower().replace("_", "-"))
    scope = str(spec.get("scope") or "")
    section = str(spec.get("codex_section") or "")
    basis = str(spec.get("authority_basis") or "A229|A230")
    conn.execute(
        "insert or replace into law_structure_directory "
        "(law_code, canonical_name, tier, owner, parent_law_code, scope, authority_basis, precedence, "
        "official_entry, introduced_version, retired_version) values (?,?,?,?,?,?,?,?,?,?,?)",
        (law, canonical, "special-law", "permission-sovereign", "CODEX_MAIN", scope, basis,
         "special-over-general-within-declared-scope", "governance-codex://official", version, None),
    )
    conn.execute(
        "insert or replace into special_law_directory "
        "(special_law_code, canonical_name, owner, codex_section, scope, general_provision_links, "
        "special_provision_links, precedence, authority_boundary, introduced_version, retired_version) "
        "values (?,?,?,?,?,?,?,?,?,?,?)",
        (law, canonical, "permission-sovereign", section, scope, "", "",
         "special-over-general-within-declared-scope",
         "remains inside the single governance codex and creates no second authority", version, None),
    )


def apply_re_tiering(
    conn: sqlite3.Connection,
    plan: Mapping[str, Any],
    *,
    version: str,
    executed: Sequence[str] = (),
    artifacts: Mapping[str, str] | None = None,
) -> OperationResult:
    """Apply a re-tiering plan to a staged generation.

    * ``xingcheng-special-law`` targets migrate law classification when the
      plan declares ``target_law``; otherwise they are recorded as
      retain-with-planned-successor.
    * ``implementation-obligation`` targets insert obligations + classification.
    * other targets are migrated to ``superseded`` when their successor
      artifact exists (``artifacts`` maps target_code -> path); otherwise they
      are recorded as retain-with-planned-successor until the artifact exists.
    """
    if not version:
        raise ConvergenceError("VERSION_REQUIRED")
    laws = dict(plan.get("laws") or {})
    artifact_map = dict(artifacts or {})
    done = set(executed)
    special = obligation = recorded = artifact_backed = 0
    for entry in plan["entries"]:
        pid = str(entry.get("provision_id") or "")
        if not pid or pid in done:
            continue
        target_type = str(entry.get("target_type") or "")
        target_code = str(entry.get("target_code") or "")
        target_law = entry.get("target_law")
        if target_type == "xingcheng-special-law" and target_law:
            _ensure_special_law(conn, str(target_law), laws.get(str(target_law)) or {}, version)
            conn.execute(
                "update provision_law_classification set tier='special-law', law_code=? "
                "where provision_type='article' and provision_id=?",
                (target_law, pid),
            )
            _migration(conn, pid, str(target_law), "special-governs", None, "normalized", version)
            row = conn.execute(
                "select special_provision_links from special_law_directory where special_law_code=?",
                (str(target_law),),
            ).fetchone()
            links = [item for item in ((row[0] or "").split("|") if row else []) if item]
            if pid not in links:
                links.append(pid)
                conn.execute(
                    "update special_law_directory set special_provision_links=? where special_law_code=?",
                    ("|".join(links), str(target_law)),
                )
            special += 1
        elif target_type == "implementation-obligation":
            conn.execute(
                "insert or replace into implementation_obligations "
                "(obligation_code, declaration_provision, implementation_owner, target_state, current_state, "
                "deadline_rule, acceptance_evidence, noncompliance_effect, waiver_rule, introduced_version, "
                "created_at_utc, due_at_utc, previous_state, verification_owner, escalation_rule) "
                "values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (target_code, pid, "system-runtime-sovereign", str(entry.get("subject") or target_code),
                 "mandated", "before-next-verified-release", "obligation-closure-evidence",
                 "OBLIGATION_CLOSURE=INCOMPLETE_EVIDENCE|VERIFIED_RELEASE_DENIED", "none",
                 version, version, None, None, "permission-sovereign", "release-blocking"),
            )
            conn.execute(
                "insert or replace into obligation_classification "
                "(obligation_code, classification, evidence_basis, version_identity, status) values (?,?,?,?,?)",
                (target_code, "IMPLEMENTATION_REMAINS", "canonical-obligation-lifecycle", version, "current"),
            )
            _migration(conn, pid, "CODEX_MAIN", "superseded", target_code, "normalized", version)
            obligation += 1
        else:
            artifact_path = artifact_map.get(target_code)
            if artifact_path and (PROJECT_ROOT / artifact_path).exists():
                _migration(conn, pid, "CODEX_MAIN", "superseded", target_code, "artifact-backed", version)
                artifact_backed += 1
            else:
                _migration(conn, pid, "CODEX_MAIN", "retain", target_code, "successor-artifact-pending", version)
                recorded += 1
    conn.commit()
    conn.execute("update metadata set value=? where key='codex_version'", (version,))
    conn.commit()
    return OperationResult(
        operation="re-tiering",
        applied=special + obligation + recorded + artifact_backed,
        details={"special-law": special, "obligation": obligation, "retained": recorded,
                 "artifact-backed": artifact_backed},
    )


def apply_formal_rule_disposition(
    conn: sqlite3.Connection,
    *,
    retire: Sequence[str],
    version: str,
) -> OperationResult:
    """Retire non-evaluator rules; leave the rest pending parity."""
    for rule in retire:
        conn.execute(
            "update formal_rule_registry set status='retired', parity_status='RETIRED_AS_NON_EVALUATOR', "
            "version_identity=? where rule_code=?",
            (version, rule),
        )
    conn.execute(
        "update formal_rule_registry set status='declared-pending-evaluator-parity', version_identity=? "
        "where status='proposed'",
        (version,),
    )
    conn.commit()
    return OperationResult("formal-rule-disposition", applied=len(retire), details={"retired": list(retire)})


def apply_closures(conn: sqlite3.Connection, closures: Sequence[Mapping[str, Any]], *, version: str) -> OperationResult:
    """Insert/update convergence closure states."""
    for item in closures:
        code = str(item.get("component_code") or "")
        state = str(item.get("governance_state") or "INCOMPLETE_EVIDENCE")
        if state not in ("PASS", "INCOMPLETE_EVIDENCE", "FAIL"):
            raise ConvergenceError(f"CLOSURE_STATE_INVALID:{code}:{state}")
        conn.execute(
            "insert or replace into governance_closure_state "
            "(component_code, structural_state, governance_state, certification_state, evidence_summary, "
            "pending_codes, version_identity, status, priority) values (?,?,?,?,?,?,?,?,?)",
            (code, str(item.get("structural_state") or "registered"), state,
             str(item.get("certification_state") or "CODEX_CERTIFICATION_INCOMPLETE"),
             str(item.get("evidence_summary") or ""), str(item.get("pending_codes") or ""),
             version, "active", str(item.get("priority") or "P0")),
        )
    conn.commit()
    return OperationResult("closures", applied=len(closures))


def validate_staged(staged: StagedGeneration) -> tuple[str, ...]:
    """Integrity + mirror-render validation of a staged generation."""
    import sys

    sys.path.insert(0, str(PROJECT_ROOT))
    from governance_rule.execution.codex_update_validation import (
        foreign_key_violations,
        validate_database_integrity,
    )

    errors: list[str] = []
    conn = staged.connect()
    try:
        baseline = tuple(foreign_key_violations(conn))
        errors.extend(validate_database_integrity(conn, baseline_violations=baseline))
    finally:
        conn.close()
    return tuple(errors)


def publish(staged: StagedGeneration, *, approve: bool = False) -> Path:
    """Copy a validated staged generation onto the live codex path."""
    if not approve:
        raise ConvergenceError("PUBLISH_REQUIRES_APPROVE")
    errors = validate_staged(staged)
    if errors:
        raise ConvergenceError("STAGED_INVALID:" + ";".join(errors[:3]))
    shutil.copy2(staged.path, staged.source)
    return staged.source


def version_axis_report(database: Path = LIVE_CODEX) -> dict[str, Any]:
    conn = sqlite3.connect(f"file:{Path(database).as_posix()}?mode=ro&immutable=1", uri=True)
    try:
        meta = dict(conn.execute("select key, value from metadata"))
        return {
            "codex_version": meta.get("codex_version"),
            "current_version": meta.get("current_version"),
            "active_provision_binding_version": meta.get("active_provision_binding_version"),
            "current_version_identity": meta.get("current_version_identity"),
            "seal_version": conn.execute("select version from seal_manifest order by rowid desc limit 1").fetchone()[0],
            "revision_head": conn.execute("select entry_hash from revision_history order by sequence desc limit 1").fetchone()[0],
            "current": meta.get("codex_version") == meta.get("current_version"),
            "closure": "PASS" if meta.get("codex_version") == meta.get("current_version") else "INCOMPLETE_EVIDENCE",
        }
    finally:
        conn.close()


def projection_status(database: Path = LIVE_CODEX) -> dict[str, Any]:
    conn = sqlite3.connect(f"file:{Path(database).as_posix()}?mode=ro&immutable=1", uri=True)
    try:
        codex_version = dict(conn.execute("select key, value from metadata")).get("codex_version")
        search = conn.execute(
            "select codex_version_identity from codex_search_index_manifest order by rowid desc limit 1").fetchone()
        module = conn.execute(
            "select version_identity from codex_internal_module_manifest order by rowid desc limit 1").fetchone()
        surface = dict(conn.execute("select key, value from metadata")).get("governance_closure_current_version")
        return {
            "codex_version": codex_version,
            "search_manifest": search[0] if search else None,
            "internal_module_manifest": module[0] if module else None,
            "normative_surface": surface,
            "search_current": bool(search and search[0] == codex_version),
            "module_manifest_current": bool(module and module[0] == codex_version),
            "surface_current": bool(surface == codex_version),
        }
    finally:
        conn.close()


def compute_closures(database: Path = LIVE_CODEX) -> dict[str, str]:
    """Evaluate the convergence closures from live evidence (framework view)."""
    axes = version_axis_report(database)
    projections = projection_status(database)
    closures: dict[str, str] = {}
    closures["VERSION_CURRENTNESS_CLOSURE"] = axes["closure"]
    closures["SEARCH_CURRENTNESS_CLOSURE"] = "PASS" if projections["search_current"] else "INCOMPLETE_EVIDENCE"
    closures["CURRENT_NORMATIVE_SURFACE_CLOSURE"] = "PASS" if projections["surface_current"] else "INCOMPLETE_EVIDENCE"
    conn = sqlite3.connect(f"file:{Path(database).as_posix()}?mode=ro&immutable=1", uri=True)
    try:
        closures["MACHINE_SCHEMA_PARITY_CLOSURE"] = (
            "PASS"
            if conn.execute("select count(*) from machine_schema_registry where parity_status!='PENDING'").fetchone()[0]
            == conn.execute("select count(*) from machine_schema_registry").fetchone()[0]
            else "INCOMPLETE_EVIDENCE"
        )
        closures["OBLIGATION_CLOSURE"] = (
            "PASS"
            if conn.execute(
                "select count(*) from implementation_obligations where current_state like 'complete%'"
            ).fetchone()[0]
            == conn.execute("select count(*) from implementation_obligations").fetchone()[0]
            else "INCOMPLETE_EVIDENCE"
        )
        closures["MIRROR_QUALITY_CLOSURE"] = (
            "PASS"
            if conn.execute(
                "select replacement_character_count + question_loss_field_count from chinese_mirror_quality_evidence "
                "where status='current'"
            ).fetchone()[0] == 0
            else "FAIL"
        )
    finally:
        conn.close()
    for code in (
        "CORE_ENGINE_EQUIVALENCE_CLOSURE",
        "SUB_SOVEREIGN_RETIREMENT_CLOSURE",
        "CAPABILITY_DISPATCH_CLOSURE",
        "FORMAL_RULE_PARITY_CLOSURE",
        "CODEX_CONVERGENCE_CLOSURE",
        "FINAL_PROJECT_CONVERGENCE_CLOSURE",
    ):
        closures.setdefault(code, "INCOMPLETE_EVIDENCE")
    return closures


__all__ = [
    "CONVERGENCE_DIR",
    "ConvergenceError",
    "OperationResult",
    "StagedGeneration",
    "apply_closures",
    "apply_formal_rule_disposition",
    "apply_re_tiering",
    "compute_closures",
    "load_re_tiering_plan",
    "projection_status",
    "publish",
    "stage_copy",
    "validate_staged",
    "version_axis_report",
]
