"""Configuration contract + runtime path contract tests (isolated).

Verifies required configuration exists with pinned identities, database
schema compatibility, model registry presence, secret references (names
only — values never read), no secret values in the manifest, and the eight
declared runtime roots (no reliance on the process working directory).
Synthetic stores live in temporary directories; the shipped contract is
validated read-only.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SHARED_SRC = ROOT / "shared-layer" / "src"
if str(SHARED_SRC) not in sys.path:
    sys.path.insert(0, str(SHARED_SRC))

from governance_rule.execution.integrity.python_release_dependencies import (  # noqa: E402
    validate_config_contract,
    validate_runtime_path_contract,
)
from shared_layer.database import release_manifest  # noqa: E402


def _file(tmp_path: Path, relative: str, content: str = "{}\n") -> Path:
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _contract(tmp_path: Path, **overrides: object) -> dict[str, object]:
    config = _file(tmp_path, "main-system/config/a.json")
    manifest = _file(
        tmp_path,
        "shared-layer/database-release.json",
        json.dumps({"minimum_runtime_version": "1.0.0"}),
    )
    lifecycle = _file(tmp_path, "models/lifecycle.json")
    pin = _file(tmp_path, "settings/native-engine.json")
    payload: dict[str, object] = {
        "config_contract": {
            "required_files": {
                "main-system/config/a.json": _sha(config),
                "shared-layer/database-release.json": _sha(manifest),
            },
            "database_schema": {
                "manifest": "shared-layer/database-release.json",
                "runtime_version": "1.0.0",
            },
            "secret_references": ["GPTBRIDGE_POSTGRES_DSN"],
            "model_registry": {
                "lifecycle": "models/lifecycle.json",
                "engine_pin": "settings/native-engine.json",
            },
        }
    }
    payload["config_contract"].update(overrides)  # type: ignore[union-attr]
    return payload


def test_shipped_contract_passes_read_only() -> None:
    contract = release_manifest.load_dependency_contract()
    assert validate_config_contract(contract, repo_root=ROOT) == []
    assert validate_runtime_path_contract(contract, repo_root=ROOT) == []


def test_missing_config_file_rejected(tmp_path: Path) -> None:
    contract = _contract(tmp_path)
    (tmp_path / "main-system" / "config" / "a.json").unlink()
    errors = validate_config_contract(contract, repo_root=tmp_path)
    assert "CONFIG_FILE_MISSING:main-system/config/a.json" in errors


def test_config_hash_mismatch_rejected(tmp_path: Path) -> None:
    contract = _contract(tmp_path)
    _file(tmp_path, "main-system/config/a.json", '{"changed": true}\n')
    errors = validate_config_contract(contract, repo_root=tmp_path)
    assert "CONFIG_HASH_MISMATCH:main-system/config/a.json" in errors


def test_database_schema_incompatible_rejected(tmp_path: Path) -> None:
    contract = _contract(tmp_path)
    _file(
        tmp_path,
        "shared-layer/database-release.json",
        json.dumps({"minimum_runtime_version": "2.0.0"}),
    )
    errors = validate_config_contract(contract, repo_root=tmp_path)
    assert any(error.startswith("DB_SCHEMA_INCOMPATIBLE") for error in errors)


def test_model_registry_missing_rejected(tmp_path: Path) -> None:
    contract = _contract(tmp_path)
    (tmp_path / "models" / "lifecycle.json").unlink()
    errors = validate_config_contract(contract, repo_root=tmp_path)
    assert any(error.startswith("MODEL_REGISTRY_MISSING:lifecycle") for error in errors)


def test_secret_reference_missing_rejected(tmp_path: Path) -> None:
    contract = _contract(tmp_path)
    errors = validate_config_contract(
        contract, repo_root=tmp_path, env={}, check_secret_references=True
    )
    assert "SECRET_REFERENCE_MISSING:GPTBRIDGE_POSTGRES_DSN" in errors


def test_secret_value_in_manifest_rejected(tmp_path: Path) -> None:
    contract = _contract(tmp_path)
    contract["config_contract"]["endpoint_references"] = {  # type: ignore[index]
        "postgresql": "postgresql://user:pass@host/db"
    }
    errors = validate_config_contract(contract, repo_root=tmp_path)
    assert any(
        error.startswith("SECRET_VALUE_IN_MANIFEST") for error in errors
    )


def test_runtime_paths_missing_root_rejected(tmp_path: Path) -> None:
    contract = {
        "runtime_paths": {
            "SOURCE_ROOT": ".",
            "RELEASE_ROOT": ".",
            "SHARED_RUNTIME_ROOT": "shared-layer",
            "PERSISTENT_DATA_ROOT": "main-system/runtime",
            "LOG_ROOT": "main-system/runtime/logs",
            "BACKUP_ROOT": "main-system/runtime/backups",
            "MODEL_ROOT": "missing/models",
            "CONFIG_ROOT": "main-system/config",
        }
    }
    (tmp_path / "shared-layer").mkdir(parents=True)
    (tmp_path / "main-system" / "runtime").mkdir(parents=True)
    (tmp_path / "main-system" / "config").mkdir(parents=True)
    errors = validate_runtime_path_contract(contract, repo_root=tmp_path)
    assert any(error.startswith("PATH_ROOT_MISSING:MODEL_ROOT") for error in errors)


def test_runtime_path_escapes_source_root_rejected(tmp_path: Path) -> None:
    contract = {
        "runtime_paths": {
            "SOURCE_ROOT": ".",
            "RELEASE_ROOT": ".",
            "SHARED_RUNTIME_ROOT": "shared-layer",
            "PERSISTENT_DATA_ROOT": "main-system/runtime",
            "LOG_ROOT": "main-system/runtime/logs",
            "BACKUP_ROOT": "main-system/runtime/backups",
            "MODEL_ROOT": str(tmp_path.parent / "outside" / "models"),
            "CONFIG_ROOT": "main-system/config",
        }
    }
    for relative in ("shared-layer", "main-system/runtime", "main-system/config"):
        (tmp_path / relative).mkdir(parents=True, exist_ok=True)
    errors = validate_runtime_path_contract(contract, repo_root=tmp_path)
    assert any(
        error.startswith("PATH_ESCAPES_SOURCE_ROOT:MODEL_ROOT") for error in errors
    )


def test_runtime_path_undeclared_root_rejected(tmp_path: Path) -> None:
    contract = {"runtime_paths": {"SOURCE_ROOT": "."}}
    errors = validate_runtime_path_contract(contract, repo_root=tmp_path)
    assert any(error.startswith("PATH_ROOT_UNDECLARED") for error in errors)
