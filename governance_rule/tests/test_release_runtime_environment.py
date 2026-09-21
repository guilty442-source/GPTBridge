"""Release runtime-environment tests (isolated synthetic releases only).

Acceptance list: verified runtime loads; dev package/source changes do not
affect a packaged release; missing dependency / version mismatch / ABI
mismatch / missing native DLL are rejected; PYTHONPATH and user-site
pollution are detected; releases do not share mutable code; the original
backend and codex validation flow are untouched; releases carry no secrets
or official authority data.  The running official backend is never modified.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SHARED_SRC = ROOT / "shared-layer" / "src"
if str(SHARED_SRC) not in sys.path:
    sys.path.insert(0, str(SHARED_SRC))

from governance_rule.execution.integrity.package_integrity import (  # noqa: E402
    collect_file_hashes,
    snapshot_digest,
)
from governance_rule.execution.integrity.python_release_dependencies import (  # noqa: E402
    probe_runtime_environment,
    validate_forbidden_release_content,
    validate_governance_references,
    validate_release_bundle,
    validate_runtime_environment,
)
from shared_layer.database import release_manifest  # noqa: E402

VENV_ROOT = Path(sys.executable).resolve().parents[1]
CODEX = ROOT / "governance_rule" / "codex" / "data" / "governance_codex.sqlite3"


def _write_package(root: Path, name: str, version: str | None = None) -> Path:
    package = root / name
    package.mkdir(parents=True, exist_ok=True)
    (package / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    if version:
        dist_info = root / f"{name}-{version}.dist-info"
        dist_info.mkdir(parents=True, exist_ok=True)
        (dist_info / "METADATA").write_text(
            f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n",
            encoding="utf-8",
        )
    return package


def _fake_pe(path: Path, machine: int = 0x8664) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    header = bytearray(0x80)
    header[0:2] = b"MZ"
    header[0x3C:0x40] = (0x40).to_bytes(4, "little")
    header[0x40:0x44] = b"PE\x00\x00"
    header[0x44:0x46] = machine.to_bytes(2, "little")
    path.write_bytes(bytes(header))
    return path


def _base_contract(modules: list[dict[str, object]]) -> dict[str, object]:
    return {
        "contract_version": 1,
        "classes": dict(release_manifest.DEPENDENCY_CLASSES),
        "modules": modules,
        "runtime_environment": {
            "python": {"version_range": {"min": [3, 11], "max_exclusive": [3, 12]}},
            "venv": {
                "required": True,
                "include_system_site_packages": False,
                "user_site": "forbidden",
                "pythonpath": "forbidden",
                "require_built_at_final_location": False,
            },
        },
        "shared_layer_classification": [
            {
                "path": "shared-layer/src/shared_layer/contracts",
                "class": "RUNTIME_CONTRACT",
                "evidence": "canonical type registry",
            }
        ],
    }


def _validate(
    contract: dict[str, object],
    *,
    release_root: Path,
    extra_paths: list[Path],
    source_root: Path | None = None,
    env: dict[str, str] | None = None,
    check_forbidden_content: bool = False,
) -> dict[str, object]:
    return validate_release_bundle(
        contract,
        python_executable=sys.executable,
        release_root=release_root,
        source_root=source_root,
        extra_paths=extra_paths,
        allowed_dependency_roots=[VENV_ROOT],
        check_forbidden_content=check_forbidden_content,
        env=env,
    )


def test_correct_runtime_loads(tmp_path: Path) -> None:
    release = tmp_path / "releaseA"
    _write_package(release, "pkg_release")
    contract = _base_contract(
        [
            {
                "module": "pkg_release",
                "class": "RELEASE_CODE",
                "required": True,
                "allowed_roots": ["."],
            }
        ]
    )
    result = _validate(contract, release_root=release, extra_paths=[release])
    assert result["ok"] is True, result["errors"]
    assert result["python"]["version"].startswith("3.11")


def test_dev_package_update_does_not_affect_release(tmp_path: Path) -> None:
    release = tmp_path / "releaseA"
    dev = tmp_path / "devVenv"
    _write_package(release, "pkg_release")
    _write_package(dev, "dev_dep", version="1.0.0")
    contract = _base_contract(
        [
            {
                "module": "pkg_release",
                "class": "RELEASE_CODE",
                "required": True,
                "allowed_roots": ["."],
            }
        ]
    )
    before = collect_file_hashes(release, ["."])
    _write_package(dev, "dev_dep", version="9.9.9")
    result = _validate(contract, release_root=release, extra_paths=[release])
    assert result["ok"] is True, result["errors"]
    after = collect_file_hashes(release, ["."])
    assert snapshot_digest(after) == snapshot_digest(before)


def test_dev_source_change_does_not_affect_release(tmp_path: Path) -> None:
    release = tmp_path / "releaseA"
    source = tmp_path / "sourceTree"
    _write_package(release, "pkg_release")
    _write_package(source, "official_pkg")
    contract = _base_contract(
        [
            {
                "module": "pkg_release",
                "class": "RELEASE_CODE",
                "required": True,
                "allowed_roots": ["."],
            }
        ]
    )
    first = _validate(
        contract, release_root=release, extra_paths=[release], source_root=source
    )
    assert first["ok"] is True
    (source / "official_pkg" / "__init__.py").write_text("VALUE = 2\n", encoding="utf-8")
    second = _validate(
        contract, release_root=release, extra_paths=[release], source_root=source
    )
    assert second["ok"] is True


def test_missing_dependency_rejected(tmp_path: Path) -> None:
    release = tmp_path / "releaseA"
    _write_package(release, "pkg_release")
    contract = _base_contract(
        [
            {
                "module": "pkg_release",
                "class": "RELEASE_CODE",
                "required": True,
                "allowed_roots": ["."],
            },
            {
                "module": "absent_pkg",
                "class": "RELEASE_DEPENDENCY",
                "required": True,
                "allowed_roots": ["."],
            },
        ]
    )
    result = _validate(contract, release_root=release, extra_paths=[release])
    assert result["ok"] is False
    assert any(
        error.startswith("REQUIRED_MODULE_MISSING:absent_pkg")
        for error in result["errors"]
    )


def test_version_mismatch_rejected(tmp_path: Path) -> None:
    release = tmp_path / "releaseA"
    _write_package(release, "dep_pkg", version="1.0.0")
    contract = _base_contract(
        [
            {
                "module": "dep_pkg",
                "class": "RELEASE_DEPENDENCY",
                "required": True,
                "allowed_roots": ["."],
                "version": "2.0.0",
            }
        ]
    )
    result = _validate(contract, release_root=release, extra_paths=[release])
    assert result["ok"] is False
    assert any(
        error.startswith("DEPENDENCY_VERSION_MISMATCH:dep_pkg")
        for error in result["errors"]
    )


def test_python_abi_and_arch_mismatch_rejected(tmp_path: Path) -> None:
    release = tmp_path / "releaseA"
    _write_package(release, "pkg_release")
    wrong_abi = release / "native" / "mod.cp310-win_amd64.pyd"
    _fake_pe(wrong_abi, 0x8664)
    wrong_arch = release / "native" / "mod.cp311-win_amd64.pyd"
    _fake_pe(wrong_arch, 0x14C)
    contract = _base_contract(
        [
            {
                "module": "pkg_release",
                "class": "RELEASE_CODE",
                "required": True,
                "allowed_roots": ["."],
            }
        ]
    )
    contract["native_extensions"] = [
        {"file": "native/mod.cp310-win_amd64.pyd", "machine": "AMD64", "abi": "cp311"},
        {"file": "native/mod.cp311-win_amd64.pyd", "machine": "AMD64", "abi": "cp311"},
    ]
    result = _validate(contract, release_root=release, extra_paths=[release])
    assert result["ok"] is False
    assert any(error.startswith("NATIVE_ABI_MISMATCH") for error in result["errors"])
    assert any(error.startswith("NATIVE_ARCH_MISMATCH") for error in result["errors"])


def test_missing_native_dll_rejected(tmp_path: Path) -> None:
    release = tmp_path / "releaseA"
    _write_package(release, "pkg_release")
    contract = _base_contract(
        [
            {
                "module": "pkg_release",
                "class": "RELEASE_CODE",
                "required": True,
                "allowed_roots": ["."],
            }
        ]
    )
    contract["required_dlls"] = ["native/runtime.dll"]
    result = _validate(contract, release_root=release, extra_paths=[release])
    assert result["ok"] is False
    assert any(
        error.startswith("NATIVE_DLL_MISSING") for error in result["errors"]
    )


def test_pythonpath_pollution_detected(tmp_path: Path) -> None:
    dev = tmp_path / "devSource"
    dev.mkdir()
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(dev)
    probe = probe_runtime_environment(
        sys.executable, env=environment, isolated=False
    )
    contract = _base_contract([])
    result = validate_runtime_environment(
        contract,
        probe,
        release_root=tmp_path / "releaseA",
        allowed_dependency_roots=[VENV_ROOT],
    )
    assert result["ok"] is False
    assert any(
        error.startswith("PYTHONPATH_POLLUTION") for error in result["errors"]
    )
    clean = probe_runtime_environment(sys.executable, isolated=True)
    clean_result = validate_runtime_environment(
        contract,
        clean,
        release_root=tmp_path / "releaseA",
        allowed_dependency_roots=[VENV_ROOT],
    )
    assert clean_result["ok"] is True, clean_result["errors"]


def test_user_site_pollution_detected(tmp_path: Path) -> None:
    contract = _base_contract([])
    polluted = {
        "version": [3, 11, 9],
        "arch": "AMD64",
        "bits": 64,
        "user_site_enabled": True,
        "user_site": str(tmp_path / "userSite"),
        "sys_path": [str(tmp_path / "userSite")],
        "site_packages": [str(VENV_ROOT / "Lib" / "site-packages")],
        "env_pythonpath": "",
        "pyvenv_cfg": None,
    }
    result = validate_runtime_environment(
        contract,
        polluted,
        release_root=tmp_path / "releaseA",
        allowed_dependency_roots=[VENV_ROOT],
    )
    assert result["ok"] is False
    assert any(error == "USER_SITE_ENABLED" for error in result["errors"])
    assert any(
        error.startswith("USER_SITE_POLLUTION") for error in result["errors"]
    )
    real = probe_runtime_environment(sys.executable, isolated=True)
    assert real["user_site_enabled"] is False


def test_cross_release_code_not_shared(tmp_path: Path) -> None:
    release_a = tmp_path / "releaseA"
    release_b = tmp_path / "releaseB"
    _write_package(release_a, "shared_name")
    _write_package(release_b, "shared_name")
    contract_b = _base_contract(
        [
            {
                "module": "shared_name",
                "class": "RELEASE_CODE",
                "required": True,
                "allowed_roots": ["."],
            }
        ]
    )
    result = _validate(
        contract_b, release_root=release_b, extra_paths=[release_a, release_b]
    )
    assert result["ok"] is False
    assert any(
        error.startswith("ORIGIN_OUTSIDE_ALLOWED_ROOTS:shared_name")
        for error in result["errors"]
    )


def test_release_excludes_secrets_and_authority_data(tmp_path: Path) -> None:
    release = tmp_path / "releaseA"
    _write_package(release, "pkg_release")
    (release / ".env").write_text("SECRET=1\n", encoding="utf-8")
    (release / "governance_codex.sqlite3").write_bytes(b"db")
    contract = _base_contract([])
    contract["forbidden_content"] = [
        ".env",
        "*.pem",
        "*.key",
        "governance_codex.sqlite3",
    ]
    result = validate_forbidden_release_content(contract, release)
    assert result["ok"] is False
    assert any(".env" in error for error in result["errors"])
    assert any("governance_codex.sqlite3" in error for error in result["errors"])
    (release / ".env").unlink()
    (release / "governance_codex.sqlite3").unlink()
    assert validate_forbidden_release_content(contract, release)["ok"] is True
    shipped = release_manifest.load_dependency_contract()
    for pattern in ("*.gguf", "*.safetensors", "*.pt", "*.onnx"):
        assert pattern in shipped["forbidden_content"]
    (release / "model.gguf").write_bytes(b"weights")
    weights = validate_forbidden_release_content(shipped, release)
    assert weights["ok"] is False
    assert any("gguf" in error for error in weights["errors"])
    (release / "model.gguf").unlink()


def test_original_backend_and_codex_flow_unchanged(tmp_path: Path) -> None:
    pyvenv = VENV_ROOT / "pyvenv.cfg"
    before_hash = hashlib.sha256(pyvenv.read_bytes()).hexdigest()
    release = tmp_path / "releaseA"
    _write_package(release, "pkg_release")
    contract = _base_contract(
        [
            {
                "module": "pkg_release",
                "class": "RELEASE_CODE",
                "required": True,
                "allowed_roots": ["."],
            }
        ]
    )
    result = _validate(contract, release_root=release, extra_paths=[release])
    assert result["ok"] is True
    assert hashlib.sha256(pyvenv.read_bytes()).hexdigest() == before_hash

    shipped = release_manifest.load_dependency_contract()
    assert release_manifest.validate_dependency_contract(shipped) == []
    assert validate_governance_references(shipped, codex_path=CODEX) == []
    tampered = dict(shipped)
    tampered["governance_references"] = dict(shipped["governance_references"])
    tampered["governance_references"]["codex_sha256"] = "0" * 64
    assert "CODEX_HASH_MISMATCH" in validate_governance_references(
        tampered, codex_path=CODEX
    )

    audit = subprocess.run(
        [sys.executable, "-m", "governance_rule.execution.audit"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=600,
    )
    assert audit.returncode == 0, audit.stdout[-400:]
