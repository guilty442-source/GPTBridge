from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "shared-layer" / "src"))

from shared_layer.metadata_contract import (
    FIELD_CONTENT_HASH,
    FIELD_LOCATOR_ID,
    FIELD_MODULE_ID,
    FIELD_RESOURCE_ID,
    FIELD_STATUS,
    FIELD_UPDATED_AT,
    FIELD_VERSION,
    QDRANT_REQUIRED_PAYLOAD_FIELDS,
    REQUIRED_FIELDS,
    STATUS_ACTIVE,
    STATUS_ARCHIVED,
    STATUS_DELETED,
    STATUS_INDEXED,
    STATUS_MISSING,
    STATUS_PENDING,
    STATUS_VALUES,
    XINGCHENG_FORBIDDEN_CLASSIFICATIONS,
    ResourceMetadata,
    validate_qdrant_payload,
)
from shared_layer.resource_identity import (
    ResourceIdentity,
    locator_id_for,
    point_id_for,
)


# ---------------------------------------------------------------------------
# locator_id / point_id canonical formulas
# ---------------------------------------------------------------------------

def test_locator_id_formula_is_deterministic() -> None:
    a = locator_id_for("xingcheng", "doc-abc123")
    b = locator_id_for("xingcheng", "doc-abc123")
    assert a == b


def test_locator_id_differs_by_module() -> None:
    a = locator_id_for("xingcheng", "doc-abc123")
    b = locator_id_for("vaultly", "doc-abc123")
    assert a != b


def test_locator_id_differs_by_resource() -> None:
    a = locator_id_for("xingcheng", "doc-abc123")
    b = locator_id_for("xingcheng", "doc-def456")
    assert a != b


def test_point_id_formula_is_deterministic() -> None:
    a = point_id_for("chunk-1")
    b = point_id_for("chunk-1")
    assert a == b


def test_point_id_differs_by_chunk() -> None:
    a = point_id_for("chunk-1")
    b = point_id_for("chunk-2")
    assert a != b


# ---------------------------------------------------------------------------
# ResourceMetadata contract
# ---------------------------------------------------------------------------

def _valid_metadata(**overrides) -> ResourceMetadata:
    defaults = dict(
        module_id="xingcheng",
        resource_id="doc-abc123",
        locator_id=str(locator_id_for("xingcheng", "doc-abc123")),
        version=1,
        updated_at="2026-01-01T00:00:00Z",
        status="active",
        content_hash="a" * 64,
    )
    defaults.update(overrides)
    return ResourceMetadata(**defaults)


def test_resource_metadata_valid() -> None:
    m = _valid_metadata()
    assert m.module_id == "xingcheng"
    assert m.resource_id == "doc-abc123"
    assert m.version == 1
    assert m.status == "active"


def test_resource_metadata_missing_field_raises() -> None:
    with pytest.raises(ValueError, match="METADATA_CONTRACT_MISSING"):
        _valid_metadata(module_id="")


def test_resource_metadata_invalid_version_raises() -> None:
    with pytest.raises(ValueError, match="INVALID_VERSION"):
        _valid_metadata(version=0)


def test_resource_metadata_invalid_status_raises() -> None:
    with pytest.raises(ValueError, match="INVALID_STATUS"):
        _valid_metadata(status="bogus")


def test_resource_metadata_locator_mismatch_raises() -> None:
    with pytest.raises(ValueError, match="LOCATOR_ID_MISMATCH"):
        _valid_metadata(locator_id="00000000-0000-0000-0000-000000000000")


def test_resource_metadata_from_identity() -> None:
    identity = ResourceIdentity(
        module_id="xingcheng",
        data_category="business",
        resource_type="document",
        resource_id="doc-abc123",
    )
    m = ResourceMetadata.from_identity(
        identity,
        version=1,
        updated_at="2026-01-01T00:00:00Z",
        status="active",
    )
    assert m.module_id == "xingcheng"
    assert m.resource_id == "doc-abc123"
    assert m.locator_id == str(locator_id_for("xingcheng", "doc-abc123"))


def test_resource_metadata_as_dict_has_required_fields() -> None:
    m = _valid_metadata()
    d = m.as_dict()
    for field in REQUIRED_FIELDS:
        assert field in d, f"Missing required field: {field}"


def test_resource_metadata_as_tags_all_strings() -> None:
    m = _valid_metadata()
    tags = m.as_tags()
    for key, value in tags.items():
        assert isinstance(value, str), f"Tag {key} is not a string: {type(value)}"


# ---------------------------------------------------------------------------
# Qdrant payload validation
# ---------------------------------------------------------------------------

def _valid_qdrant_payload() -> dict:
    return {
        "module_id": "xingcheng",
        "resource_id": "chunk-abc123-1",
        "chunk_id": "abc123-1",
        "version": 1,
        "locator_id": str(locator_id_for("xingcheng", "chunk-abc123-1")),
    }


def test_qdrant_payload_valid() -> None:
    violations = validate_qdrant_payload(_valid_qdrant_payload())
    assert violations == []


def test_qdrant_payload_missing_module_id() -> None:
    payload = _valid_qdrant_payload()
    del payload["module_id"]
    violations = validate_qdrant_payload(payload)
    assert any("module_id" in v for v in violations)


def test_qdrant_payload_missing_resource_id() -> None:
    payload = _valid_qdrant_payload()
    del payload["resource_id"]
    violations = validate_qdrant_payload(payload)
    assert any("resource_id" in v for v in violations)


def test_qdrant_payload_missing_chunk_id() -> None:
    payload = _valid_qdrant_payload()
    del payload["chunk_id"]
    violations = validate_qdrant_payload(payload)
    assert any("chunk_id" in v for v in violations)


def test_qdrant_payload_missing_version() -> None:
    payload = _valid_qdrant_payload()
    del payload["version"]
    violations = validate_qdrant_payload(payload)
    assert any("version" in v for v in violations)


def test_qdrant_payload_invalid_version() -> None:
    payload = _valid_qdrant_payload()
    payload["version"] = 0
    violations = validate_qdrant_payload(payload)
    assert any("INVALID_VERSION" in v for v in violations)


def test_qdrant_required_payload_fields_contract() -> None:
    assert "module_id" in QDRANT_REQUIRED_PAYLOAD_FIELDS
    assert "resource_id" in QDRANT_REQUIRED_PAYLOAD_FIELDS
    assert "chunk_id" in QDRANT_REQUIRED_PAYLOAD_FIELDS
    assert "version" in QDRANT_REQUIRED_PAYLOAD_FIELDS
    assert "locator_id" in QDRANT_REQUIRED_PAYLOAD_FIELDS


# ---------------------------------------------------------------------------
# Status value vocabulary
# ---------------------------------------------------------------------------

def test_status_values_complete() -> None:
    expected = {"active", "indexed", "pending", "missing",
                "moving", "archived", "deleted", "referenced"}
    assert STATUS_VALUES == expected


# ---------------------------------------------------------------------------
# Xingcheng forbidden classifications
# ---------------------------------------------------------------------------

def test_xingcheng_forbidden_classifications() -> None:
    assert "permission" in XINGCHENG_FORBIDDEN_CLASSIFICATIONS
    assert "governance-rule" in XINGCHENG_FORBIDDEN_CLASSIFICATIONS


# ---------------------------------------------------------------------------
# Reconcile service (SQLite-only, no PostgreSQL)
# ---------------------------------------------------------------------------

def test_reconcile_pending_without_postgresql(tmp_path: Path) -> None:
    from shared_layer.reconcile import ReconcileService
    conn = sqlite3.connect(str(tmp_path / "test.sqlite3"))
    service = ReconcileService(conn, pg_connection=None)
    service.mark_pending("xingcheng", "doc-1", version=2,
                         updated_at="2026-01-01T00:00:00Z",
                         content_hash="b" * 64)
    assert service.pending_count("xingcheng") == 1
    results = list(service.reconcile_module("xingcheng"))
    assert len(results) == 1
    assert results[0].action == "skipped"
    assert results[0].detail == "postgresql-unavailable"
    conn.close()


def test_reconcile_mark_and_count(tmp_path: Path) -> None:
    from shared_layer.reconcile import ReconcileService
    conn = sqlite3.connect(str(tmp_path / "test.sqlite3"))
    service = ReconcileService(conn, pg_connection=None)
    service.mark_pending("xingcheng", "doc-1", 1, "2026-01-01T00:00:00Z")
    service.mark_pending("xingcheng", "doc-2", 1, "2026-01-01T00:00:00Z")
    service.mark_pending("vaultly", "doc-3", 1, "2026-01-01T00:00:00Z")
    assert service.pending_count("xingcheng") == 2
    assert service.pending_count("vaultly") == 1
    assert service.pending_count() == 3
    conn.close()


# ---------------------------------------------------------------------------
# SQL migration files exist
# ---------------------------------------------------------------------------

def test_migration_files_exist() -> None:
    migrations = ROOT / "shared-layer" / "migrations"
    assert (migrations / "004_global_and_module_version_tables.sql").is_file()
    assert (migrations / "005_central_index_composite_indexes.sql").is_file()
    assert (migrations / "006_audit_append_only_enforcement.sql").is_file()
    assert (migrations / "007_transport_idempotency_key.sql").is_file()
    assert (migrations / "008_rls_role_isolation.sql").is_file()


def test_sqlite_module_template_exists() -> None:
    template = ROOT / "shared-layer" / "sql" / "sqlite_module_template.sql"
    assert template.is_file()
    content = template.read_text(encoding="utf-8")
    assert "schema_version" in content
    assert "module_metadata" in content
    assert "resource_metadata" in content
    assert "audit_event" in content
    assert "reconcile_state" in content


def test_data_ownership_contract_exists() -> None:
    doc = ROOT / "shared-layer" / "docs" / "DATA_OWNERSHIP_CONTRACT.md"
    assert doc.is_file()
    content = doc.read_text(encoding="utf-8")
    assert "PostgreSQL" in content
    assert "Qdrant" in content
    assert "SQLite" in content
    # "可重建" = rebuildable in Chinese
    assert "可重建" in content or "rebuildable" in content.lower()
