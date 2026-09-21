"""G56 release manifest compatibility gates."""
from __future__ import annotations

from core_system.active_release_verify import (
    validate_backend_release_manifest,
    validate_release_manifest,
)


_REQUIRED = {
    "release_id": "release-1",
    "git_commit": "a" * 40,
    "backend_version": "1.0.0",
    "frontend_version": "1.0.0",
    "ipc_contract_version": 3,
    "database_schema_version": 12,
    "codex_schema": "gptbridge-governance-codex-v1",
    "codex_version": 2,
    "build_hash": "b" * 64,
    "status": "STAGED",
    "compatibility": {
        "codex": True,
        "permissions": True,
        "ipc": True,
        "sql_schema": True,
        "frontend": True,
    },
}


def test_g56_accepts_manifest_with_all_compatibility_gates() -> None:
    result = validate_release_manifest(
        _REQUIRED,
        expected_codex_schema="gptbridge-governance-codex-v1",
        expected_codex_version=2,
    )
    assert result["ok"] is True
    assert result["failed_gates"] == []


def test_g56_fails_closed_when_a_gate_is_false() -> None:
    manifest = {**_REQUIRED, "compatibility": {**_REQUIRED["compatibility"], "ipc": False}}
    result = validate_release_manifest(manifest)
    assert result["ok"] is False
    assert "ipc" in result["failed_gates"]


def test_g74_accepts_backend_manifest_with_matching_ui_contract() -> None:
    result = validate_backend_release_manifest(
        _REQUIRED,
        ui_ipc_contract_version=3,
        ui_release_id="release-1",
        expected_codex_schema="gptbridge-governance-codex-v1",
        expected_codex_version=2,
    )
    assert result["ok"] is True
    assert result["backend_version"] == "1.0.0"


def test_g74_fails_closed_on_ui_backend_contract_mismatch() -> None:
    result = validate_backend_release_manifest(
        _REQUIRED,
        ui_ipc_contract_version=2,
        ui_release_id="release-old",
    )
    assert result["ok"] is False
    assert "ipc_contract_version" in result["failed_gates"]
    assert "release_id" in result["failed_gates"]


def test_g56_rejects_identity_drift_and_missing_fields() -> None:
    manifest = {
        **_REQUIRED,
        "frontend_version": "2.0.0",
        "codex_schema": "old-schema",
        "build_hash": None,
    }
    del manifest["ipc_contract_version"]
    result = validate_release_manifest(
        manifest,
        expected_codex_schema="gptbridge-governance-codex-v1",
        expected_codex_version=2,
    )
    assert result["ok"] is False
    assert "ipc_contract_version" in result["missing"]
    assert "frontend_backend_version" in result["failed_gates"]
    assert "codex_schema" in result["failed_gates"]
