"""Tests for §10.19 release bundle layout — dev/prod separation (W1-5)."""

from __future__ import annotations

import json
from pathlib import Path

from core_system.release_layout import (
    REQUIRED_ENTRIES,
    SCHEMA_VERSION,
    build_release_payload_snapshot,
    check_release_layout,
)


def _valid_bundle(root: Path, **manifest_overrides) -> Path:
    (root / "backend").mkdir(parents=True)
    (root / "backend" / "main.py").write_text("print('ok')\n", encoding="utf-8")
    (root / "dependencies").mkdir()
    manifest = {
        "release_id": "release-A",
        "required_contracts": ["ipc_contract", "sql_schema"],
        "dependency_lock": {"identity": "uv-lock-sha256:abc"},
        "build_metadata": {"built_at": "2026-09-21", "builder": "w4-0c"},
        "payload_snapshot": build_release_payload_snapshot(root),
    }
    manifest.update(manifest_overrides)
    (root / "manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    return root


def test_valid_bundle_passes(tmp_path: Path) -> None:
    result = check_release_layout(_valid_bundle(tmp_path / "release-A"))
    assert result["schema_version"] == SCHEMA_VERSION
    assert result["ok"] is True
    assert result["missing"] == []
    assert result["forbidden"] == []
    assert result["manifest_errors"] == []


def test_missing_root_fails_closed(tmp_path: Path) -> None:
    result = check_release_layout(tmp_path / "does-not-exist")
    assert result["ok"] is False
    assert result["reason"] == "release-root-missing"
    assert set(result["missing"]) == set(REQUIRED_ENTRIES)


def test_missing_backend_directory_rejected(tmp_path: Path) -> None:
    root = _valid_bundle(tmp_path / "r")
    (root / "backend" / "main.py").unlink()
    (root / "backend").rmdir()
    result = check_release_layout(root)
    assert result["ok"] is False
    assert "backend" in result["missing"]


def test_missing_dependencies_directory_rejected(tmp_path: Path) -> None:
    root = _valid_bundle(tmp_path / "r")
    (root / "dependencies").rmdir()
    result = check_release_layout(root)
    assert result["ok"] is False
    assert "dependencies" in result["missing"]


def test_missing_manifest_rejected(tmp_path: Path) -> None:
    root = _valid_bundle(tmp_path / "r")
    (root / "manifest.json").unlink()
    result = check_release_layout(root)
    assert result["ok"] is False
    assert "manifest.json" in result["missing"]


def test_manifest_missing_lock_identity_rejected(tmp_path: Path) -> None:
    root = _valid_bundle(tmp_path / "r")
    manifest = json.loads((root / "manifest.json").read_text("utf-8"))
    manifest["dependency_lock"] = {}
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    result = check_release_layout(root)
    assert result["ok"] is False
    assert "MANIFEST_MISSING:dependency_lock.identity" in result["manifest_errors"]


def test_manifest_missing_build_metadata_rejected(tmp_path: Path) -> None:
    root = _valid_bundle(tmp_path / "r")
    manifest = json.loads((root / "manifest.json").read_text("utf-8"))
    manifest.pop("build_metadata")
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    result = check_release_layout(root)
    assert result["ok"] is False
    assert "MANIFEST_MISSING:build_metadata" in result["manifest_errors"]


def test_manifest_missing_required_contracts_rejected(tmp_path: Path) -> None:
    root = _valid_bundle(tmp_path / "r")
    manifest = json.loads((root / "manifest.json").read_text("utf-8"))
    manifest["required_contracts"] = []
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    result = check_release_layout(root)
    assert result["ok"] is False
    assert "MANIFEST_MISSING:required_contracts" in result["manifest_errors"]


def test_manifest_missing_payload_snapshot_rejected(tmp_path: Path) -> None:
    root = _valid_bundle(tmp_path / "r")
    manifest = json.loads((root / "manifest.json").read_text("utf-8"))
    manifest.pop("payload_snapshot")
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    result = check_release_layout(root)
    assert result["ok"] is False
    assert "MANIFEST_MISSING:payload_snapshot" in result["manifest_errors"]


def test_payload_mutation_detected(tmp_path: Path) -> None:
    root = _valid_bundle(tmp_path / "r")
    (root / "backend" / "main.py").write_text("print('mutated')\n", encoding="utf-8")
    result = check_release_layout(root)
    assert result["ok"] is False
    assert "PAYLOAD_SNAPSHOT_MISMATCH" in result["payload_errors"]
    assert "changed:backend/main.py" in result["payload_mismatches"]


def test_payload_dependency_install_detected(tmp_path: Path) -> None:
    root = _valid_bundle(tmp_path / "r")
    (root / "dependencies" / "installed.py").write_text("VALUE = 2\n", encoding="utf-8")
    result = check_release_layout(root)
    assert result["ok"] is False
    assert "PAYLOAD_SNAPSHOT_MISMATCH" in result["payload_errors"]
    assert "unexpected:dependencies/installed.py" in result["payload_mismatches"]


def test_uncovered_payload_root_rejected(tmp_path: Path) -> None:
    root = _valid_bundle(tmp_path / "r")
    (root / "extras").mkdir()
    (root / "extras" / "tool.py").write_text("VALUE = 3\n", encoding="utf-8")
    result = check_release_layout(root)
    assert result["ok"] is False
    assert "PAYLOAD_ROOT_UNCOVERED:extras" in result["payload_errors"]


def test_payload_digest_tampering_rejected(tmp_path: Path) -> None:
    root = _valid_bundle(tmp_path / "r")
    manifest = json.loads((root / "manifest.json").read_text("utf-8"))
    manifest["payload_snapshot"]["digest"] = "0" * 64
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    result = check_release_layout(root)
    assert result["ok"] is False
    assert "PAYLOAD_SNAPSHOT_INVALID:digest" in result["payload_errors"]


def test_model_weights_not_copied(tmp_path: Path) -> None:
    root = _valid_bundle(tmp_path / "r")
    (root / "dependencies" / "model.pt").write_bytes(b"\x00\x01")
    result = check_release_layout(root)
    assert result["ok"] is False
    assert any(v.endswith(":model-weights") for v in result["forbidden"])


def test_postgres_data_not_copied(tmp_path: Path) -> None:
    root = _valid_bundle(tmp_path / "r")
    (root / "pgdata").mkdir()
    (root / "pgdata" / "PG_VERSION").write_text("15\n", encoding="utf-8")
    result = check_release_layout(root)
    assert result["ok"] is False
    assert any(v.endswith(":postgres-data") for v in result["forbidden"])


def test_qdrant_index_not_copied(tmp_path: Path) -> None:
    root = _valid_bundle(tmp_path / "r")
    (root / "dependencies" / "qdrant_storage").mkdir()
    result = check_release_layout(root)
    assert result["ok"] is False
    assert any(v.endswith(":qdrant-index") for v in result["forbidden"])


def test_user_data_not_copied(tmp_path: Path) -> None:
    root = _valid_bundle(tmp_path / "r")
    (root / "user-data").mkdir()
    (root / "user-data" / "profile.json").write_text("{}", encoding="utf-8")
    result = check_release_layout(root)
    assert result["ok"] is False
    assert any(v.endswith(":user-data") for v in result["forbidden"])


def test_nested_forbidden_file_detected(tmp_path: Path) -> None:
    root = _valid_bundle(tmp_path / "r")
    nested = root / "backend" / "assets" / "deep"
    nested.mkdir(parents=True)
    (nested / "weights.safetensors").write_bytes(b"\x00")
    result = check_release_layout(root)
    assert result["ok"] is False
    assert "backend/assets/deep/weights.safetensors:model-weights" in result["forbidden"]


def test_gate_fn_reused_on_contract(tmp_path: Path) -> None:
    root = _valid_bundle(tmp_path / "r")
    manifest = json.loads((root / "manifest.json").read_text("utf-8"))
    manifest["dependency_contract"] = {"modules": []}
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    calls: list[dict] = []

    def gate_fn(contract: dict) -> list[str]:
        calls.append(contract)
        return ["ORIGIN_OUTSIDE_RELEASE:shared_layer"]

    result = check_release_layout(root, gate_fn=gate_fn)
    assert calls == [{"modules": []}]
    assert result["ok"] is False
    assert result["gate_errors"] == ["ORIGIN_OUTSIDE_RELEASE:shared_layer"]


def test_gate_fn_fail_closed_when_broken(tmp_path: Path) -> None:
    root = _valid_bundle(tmp_path / "r")
    manifest = json.loads((root / "manifest.json").read_text("utf-8"))
    manifest["dependency_contract"] = {"modules": []}
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    def gate_fn(_contract: dict) -> list[str]:
        raise RuntimeError("gate exploded")

    result = check_release_layout(root, gate_fn=gate_fn)
    assert result["ok"] is False
    assert any(e.startswith("GATE_FAILED:") for e in result["gate_errors"])