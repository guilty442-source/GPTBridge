"""Frontend release isolation tests (Main/Preload/Renderer + data isolation).

Verifies: one packaged frontend fixes the Main/Preload/Renderer combination
(artifact hashes), Electron/Node runtime identity, dependency lock, IPC
contract identity, security baseline and token-exposure policy; persistent
data / runtime config / model weights never live inside a release payload.
Synthetic repositories live in temporary directories; the shipped frontend
release is validated read-only.
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
    validate_frontend_release,
    validate_persistent_data_separation,
)
from shared_layer.database import release_manifest  # noqa: E402


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _synthetic_repo(tmp_path: Path, *, token_policy: str = "forbidden") -> dict[str, object]:
    repo = tmp_path / "repo"
    (repo / "main-system" / "dist-ui" / "main").mkdir(parents=True)
    (repo / "main-system" / "dist-ui" / "renderer").mkdir(parents=True)
    artifacts = {
        "main": "main-system/dist-ui/main/index.js",
        "preload": "main-system/dist-ui/main/preload.js",
        "renderer": "main-system/dist-ui/renderer/index.html",
    }
    hashes = {}
    for name, relative in artifacts.items():
        path = repo / relative
        path.write_text(f"// {name}\n", encoding="utf-8")
        hashes[name] = _sha(path)
    electron_dir = repo / "main-system" / "node_modules" / "electron"
    electron_dir.mkdir(parents=True)
    (electron_dir / "package.json").write_text(
        json.dumps({"version": "39.8.10"}), encoding="utf-8"
    )
    lock = repo / "main-system" / "package-lock.json"
    lock.write_text('{"lockfileVersion": 3}\n', encoding="utf-8")
    surface = repo / "main-system" / "config" / "ipc-surface-frontend.json"
    surface.parent.mkdir(parents=True)
    surface.write_text(
        json.dumps({"surface_version": "fe-surface-1"}), encoding="utf-8"
    )
    return {
        "repo": repo,
        "contract": {
            "frontend_release": {
                "electron_version": "39.8.10",
                "artifacts": {"paths": artifacts, "hashes": hashes},
                "dependency_lock": {
                    "file": "main-system/package-lock.json",
                    "sha256": _sha(lock),
                },
                "ipc_contract_identity": "fe-surface-1",
                "security": {
                    "context_isolation": True,
                    "node_integration": False,
                    "sandbox": True,
                    "channel_allowlist": True,
                    "exposes_backend_token_to_renderer": True,
                    "token_exposure_policy": token_policy,
                },
            },
            "ipc_contract": {
                "surface_pairing": {
                    "frontend_surface_file": "main-system/config/ipc-surface-frontend.json"
                }
            },
        },
    }


def test_frontend_release_passes(tmp_path: Path) -> None:
    data = _synthetic_repo(tmp_path, token_policy="acknowledged-gap-G86")
    errors = validate_frontend_release(data["contract"], repo_root=data["repo"])
    assert errors == [], errors


def test_artifact_hash_mismatch_rejected(tmp_path: Path) -> None:
    data = _synthetic_repo(tmp_path, token_policy="acknowledged-gap-G86")
    (data["repo"] / "main-system" / "dist-ui" / "main" / "preload.js").write_text(
        "// tampered\n", encoding="utf-8"
    )
    errors = validate_frontend_release(data["contract"], repo_root=data["repo"])
    assert "FRONTEND_ARTIFACT_HASH_MISMATCH:preload" in errors


def test_electron_version_mismatch_rejected(tmp_path: Path) -> None:
    data = _synthetic_repo(tmp_path, token_policy="acknowledged-gap-G86")
    (data["repo"] / "main-system" / "node_modules" / "electron" / "package.json").write_text(
        json.dumps({"version": "40.0.0"}), encoding="utf-8"
    )
    errors = validate_frontend_release(data["contract"], repo_root=data["repo"])
    assert any(
        error.startswith("FRONTEND_ELECTRON_VERSION_MISMATCH")
        for error in errors
    )


def test_lock_mismatch_rejected(tmp_path: Path) -> None:
    data = _synthetic_repo(tmp_path, token_policy="acknowledged-gap-G86")
    (data["repo"] / "main-system" / "package-lock.json").write_text(
        '{"lockfileVersion": 3, "changed": true}\n', encoding="utf-8"
    )
    errors = validate_frontend_release(data["contract"], repo_root=data["repo"])
    assert "FRONTEND_LOCK_MISMATCH" in errors


def test_token_exposure_violation(tmp_path: Path) -> None:
    data = _synthetic_repo(tmp_path, token_policy="forbidden")
    errors = validate_frontend_release(data["contract"], repo_root=data["repo"])
    assert "FRONTEND_SECURITY_VIOLATION:backend-token-exposed" in errors


def test_ipc_identity_mismatch_rejected(tmp_path: Path) -> None:
    data = _synthetic_repo(tmp_path, token_policy="acknowledged-gap-G86")
    (data["repo"] / "main-system" / "config" / "ipc-surface-frontend.json").write_text(
        json.dumps({"surface_version": "other"}), encoding="utf-8"
    )
    errors = validate_frontend_release(data["contract"], repo_root=data["repo"])
    assert "FRONTEND_IPC_IDENTITY_MISMATCH" in errors


def test_persistent_data_inside_release_rejected(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    release = repo / "resources" / "app"
    release.mkdir(parents=True)
    official = repo / "main-system"
    (official / "runtime" / "settings").mkdir(parents=True)
    contract = {
        "persistent_data_isolation": {
            "paths": ["main-system/runtime/settings"]
        },
        "runtime_config_paths": ["main-system/config"],
        "modules": [],
    }
    clean = validate_persistent_data_separation(
        contract, release_root=release, official_root=repo
    )
    assert clean == []
    inside = validate_persistent_data_separation(
        contract, release_root=repo, official_root=repo
    )
    assert any(
        error.startswith("PERSISTENT-DATA_INSIDE_RELEASE")
        for error in inside
    )


def test_persistent_data_as_module_rejected(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    release = repo / "resources" / "app"
    release.mkdir(parents=True)
    contract = {
        "persistent_data_isolation": {"paths": ["main-system/data"]},
        "runtime_config_paths": [],
        "modules": [{"module": "data"}],
    }
    errors = validate_persistent_data_separation(
        contract, release_root=release, official_root=repo
    )
    assert any(error.startswith("PERSISTENT-DATA_AS_MODULE") for error in errors)


def test_shipped_frontend_release_passes_read_only() -> None:
    contract = release_manifest.load_dependency_contract()
    assert validate_frontend_release(contract, repo_root=ROOT) == []


def test_shipped_persistent_paths_outside_packaged_release(tmp_path: Path) -> None:
    contract = release_manifest.load_dependency_contract()
    packaged = tmp_path / "resources" / "app"
    packaged.mkdir(parents=True)
    errors = validate_persistent_data_separation(
        contract, release_root=packaged, official_root=ROOT
    )
    assert errors == [], errors
