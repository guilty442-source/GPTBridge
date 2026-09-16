"""Dependency Governor (Phase I).

Runtime helpers for dependency & version governance:
- Version locking
- Compatibility matrix
- Upgrade classification
- Driver compatibility tests
- PG major upgrade rehearsal
- SQLite runtime compatibility
- Qdrant contract compatibility
- SBOM / dependency inventory
- Vulnerability & risk classification
- Dependency drift detection
- Offline bundle management
- Release signature

Codex basis:
    A46/E22 — Audit: mandatory-ledger.
    A8/E21  — PostgreSQL: central-structured-official-data.
    A10/E10 — explicit-allowlist.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any


def compute_bundle_hash(component_hashes: list[dict[str, str]]) -> str:
    """Compute aggregate SHA-256 hash from component hashes."""
    parts = []
    for item in component_hashes:
        parts.append(f"{item.get('component', '')}:{item.get('hash', '')}")
    data = "|".join(parts).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def lock_version(
    conn: Any,
    release_id: str,
    component: str,
    version_string: str,
    locked_by: str,
    major: int | None = None,
    minor: int | None = None,
    patch: int | None = None,
    description: str | None = None,
) -> str:
    """Lock a component version for a release."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT gptbridge_index.lock_version(%s, %s, %s, %s, %s, %s, %s, %s)",
            (release_id, component, version_string, major, minor, patch,
             locked_by, description),
        )
        row = cur.fetchone()
    conn.commit()
    return str(row[0]) if row else ""


def get_version_lock(
    conn: Any, release_id: str, component: str
) -> dict[str, Any] | None:
    """Get locked version for a component in a release."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT version_string, major_version, minor_version, patch_version "
            "FROM gptbridge_index.get_version_lock(%s, %s)",
            (release_id, component),
        )
        row = cur.fetchone()
    if not row:
        return None
    return {
        "version_string": row[0],
        "major_version": row[1],
        "minor_version": row[2],
        "patch_version": row[3],
    }


def record_compatibility(
    conn: Any,
    release_id: str,
    pg_version: str,
    psycopg_version: str,
    sqlite_version: str,
    qdrant_version: str,
    status: str,
    tested_by: str | None = None,
    **kwargs: Any,
) -> str:
    """Record a tested/forbidden combination."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT gptbridge_index.record_compatibility(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (release_id, pg_version, psycopg_version, sqlite_version,
             qdrant_version, status, tested_by,
             kwargs.get("qdrant_client_version"),
             kwargs.get("python_version"),
             json.dumps(kwargs["test_result"]) if "test_result" in kwargs else None,
             kwargs.get("notes")),
        )
        row = cur.fetchone()
    conn.commit()
    return str(row[0]) if row else ""


def check_combination_allowed(
    conn: Any,
    pg_version: str,
    psycopg_version: str,
    sqlite_version: str,
    qdrant_version: str,
) -> str:
    """Check if a combination is allowed (tested/forbidden/untested)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT gptbridge_index.check_combination_allowed(%s, %s, %s, %s)",
            (pg_version, psycopg_version, sqlite_version, qdrant_version),
        )
        row = cur.fetchone()
    return str(row[0]) if row else "untested"


def classify_upgrade(
    conn: Any,
    component: str,
    from_version: str,
    to_version: str,
    upgrade_class: str,
    classified_by: str,
    description: str | None = None,
) -> str:
    """Classify an upgrade as patch/minor/major."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT gptbridge_index.classify_upgrade(%s, %s, %s, %s, %s, %s)",
            (component, from_version, to_version, upgrade_class,
             classified_by, description),
        )
        row = cur.fetchone()
    conn.commit()
    return str(row[0]) if row else ""


def get_upgrade_class(
    conn: Any, component: str, from_version: str, to_version: str
) -> dict[str, Any] | None:
    """Get the classification for an upgrade."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT upgrade_class, required_validation, allows_unattended, "
            "requires_backup, requires_clone_test, requires_certification, "
            "rollback_allowed "
            "FROM gptbridge_index.get_upgrade_class(%s, %s, %s)",
            (component, from_version, to_version),
        )
        row = cur.fetchone()
    if not row:
        return None
    return {
        "upgrade_class": row[0],
        "required_validation": row[1],
        "allows_unattended": row[2],
        "requires_backup": row[3],
        "requires_clone_test": row[4],
        "requires_certification": row[5],
        "rollback_allowed": row[6],
    }


def record_driver_test(
    conn: Any,
    driver_name: str,
    driver_version: str,
    test_category: str,
    passed: bool,
    tested_by: str,
    **kwargs: Any,
) -> str:
    """Record a driver compatibility test result."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT gptbridge_index.record_driver_test(%s, %s, %s, %s, %s, %s, %s, %s)",
            (driver_name, driver_version, test_category, passed, tested_by,
             json.dumps(kwargs["test_details"]) if "test_details" in kwargs else None,
             kwargs.get("failure_reason"),
             kwargs.get("duration_ms")),
        )
        row = cur.fetchone()
    conn.commit()
    return str(row[0]) if row else ""


def is_driver_version_verified(
    conn: Any, driver_name: str, driver_version: str
) -> bool:
    """Check if all required tests passed for a driver version."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT gptbridge_index.is_driver_version_verified(%s, %s)",
            (driver_name, driver_version),
        )
        row = cur.fetchone()
    return bool(row[0]) if row else False


def start_pg_rehearsal(
    conn: Any, from_version: str, to_version: str, backup_id: str
) -> str:
    """Start a PG major upgrade rehearsal."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT gptbridge_index.start_pg_rehearsal(%s, %s, %s)",
            (from_version, to_version, backup_id),
        )
        row = cur.fetchone()
    conn.commit()
    return str(row[0]) if row else ""


def advance_pg_rehearsal(
    conn: Any,
    rehearsal_id: str,
    new_status: str,
    check_passed: bool | None = None,
    failure_reason: str | None = None,
) -> None:
    """Advance a PG rehearsal to the next step."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT gptbridge_index.advance_pg_rehearsal(%s, %s, %s, %s)",
            (rehearsal_id, new_status, check_passed, failure_reason),
        )
    conn.commit()


def record_sqlite_runtime_compat(
    conn: Any,
    python_version: str,
    sqlite_version: str,
    tested_by: str,
    fts5: bool = False,
    wal: bool = False,
    json1: bool = False,
    **kwargs: Any,
) -> str:
    """Record Python↔SQLite runtime compatibility."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT gptbridge_index.record_sqlite_runtime_compat(%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (python_version, sqlite_version, tested_by, fts5, wal, json1,
             kwargs.get("sqlite_source", "stdlib"),
             json.dumps(kwargs["test_result"]) if "test_result" in kwargs else None,
             kwargs.get("notes")),
        )
        row = cur.fetchone()
    conn.commit()
    return str(row[0]) if row else ""


def record_qdrant_compat(
    conn: Any,
    from_version: str,
    to_version: str,
    check_category: str,
    passed: bool,
    tested_by: str,
    **kwargs: Any,
) -> str:
    """Record a Qdrant compatibility check."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT gptbridge_index.record_qdrant_compat(%s, %s, %s, %s, %s, %s, %s, %s)",
            (from_version, to_version, check_category, passed, tested_by,
             json.dumps(kwargs["test_details"]) if "test_details" in kwargs else None,
             kwargs.get("failure_reason"),
             kwargs.get("migration_notes")),
        )
        row = cur.fetchone()
    conn.commit()
    return str(row[0]) if row else ""


def record_sbom_entry(
    conn: Any,
    release_id: str,
    component: str,
    component_type: str,
    version: str,
    **kwargs: Any,
) -> str:
    """Record an SBOM entry."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT gptbridge_index.record_sbom_entry(%s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (release_id, component, component_type, version,
             kwargs.get("source"), kwargs.get("source_hash"),
             kwargs.get("install_path"), kwargs.get("license_name"),
             kwargs.get("notes")),
        )
        row = cur.fetchone()
    conn.commit()
    return str(row[0]) if row else ""


def record_vulnerability(
    conn: Any,
    component: str,
    affected_versions: str,
    risk_level: str,
    recommended_action: str,
    **kwargs: Any,
) -> str:
    """Record a vulnerability."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT gptbridge_index.record_vulnerability(%s, %s, %s, %s, %s, %s, %s, %s)",
            (component, affected_versions, risk_level, recommended_action,
             kwargs.get("fixed_version"), kwargs.get("cve_id"),
             kwargs.get("upgrade_deadline_days"), kwargs.get("description")),
        )
        row = cur.fetchone()
    conn.commit()
    return str(row[0]) if row else ""


def record_dependency_drift(
    conn: Any,
    release_id: str,
    component: str,
    expected_version: str,
    installed_version: str,
    detected_by: str,
) -> str:
    """Record a dependency drift (version mismatch)."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT gptbridge_index.record_dependency_drift(%s, %s, %s, %s, %s)",
            (release_id, component, expected_version, installed_version,
             detected_by),
        )
        row = cur.fetchone()
    conn.commit()
    return str(row[0]) if row else ""


def register_offline_bundle(
    conn: Any,
    release_id: str,
    component: str,
    version: str,
    package_type: str,
    storage_locator: str,
    file_hash: str,
    **kwargs: Any,
) -> str:
    """Register an offline installation bundle."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT gptbridge_index.register_offline_bundle(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (release_id, component, version, package_type,
             storage_locator, file_hash,
             kwargs.get("platform"), kwargs.get("file_size_bytes"),
             kwargs.get("hash_algorithm", "sha256"), kwargs.get("notes")),
        )
        row = cur.fetchone()
    conn.commit()
    return str(row[0]) if row else ""


def sign_release(
    conn: Any,
    release_id: str,
    component_hashes: list[dict[str, str]],
    signed_by: str,
) -> str:
    """Sign a release with a bundle hash computed from component hashes."""
    bundle_hash = compute_bundle_hash(component_hashes)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT gptbridge_index.sign_release(%s, %s, %s, %s)",
            (release_id, bundle_hash,
             json.dumps(component_hashes), signed_by),
        )
        row = cur.fetchone()
    conn.commit()
    return str(row[0]) if row else ""


def verify_release_signature(
    conn: Any,
    signature_id: str,
    expected_bundle_hash: str,
    verified_by: str,
) -> bool:
    """Verify a release signature."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT gptbridge_index.verify_release_signature(%s, %s, %s)",
            (signature_id, expected_bundle_hash, verified_by),
        )
        row = cur.fetchone()
    conn.commit()
    return bool(row[0]) if row else False


__all__ = [
    "compute_bundle_hash",
    "lock_version",
    "get_version_lock",
    "record_compatibility",
    "check_combination_allowed",
    "classify_upgrade",
    "get_upgrade_class",
    "record_driver_test",
    "is_driver_version_verified",
    "start_pg_rehearsal",
    "advance_pg_rehearsal",
    "record_sqlite_runtime_compat",
    "record_qdrant_compat",
    "record_sbom_entry",
    "record_vulnerability",
    "record_dependency_drift",
    "register_offline_bundle",
    "sign_release",
    "verify_release_signature",
]
