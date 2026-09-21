"""Python release import-origin validation tests (isolated releases only).

Covers the release-isolation acceptance list: correct release dependency
passes, missing module fails, source-tree leakage fails, version mismatches
are not conflated, Windows path resolution is correct, the manifest matches
actual dependencies, and the original backend / codex validation flow are
untouched.  Every release used here is a synthetic temporary directory; the
running official backend is never modified.
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

from governance_rule.execution.integrity.python_release_dependencies import (  # noqa: E402
    normalize_path,
    path_is_within,
    probe_python_import_origins,
    validate_module_origins,
    validate_release_python_origins,
)
from shared_layer.database import release_manifest  # noqa: E402


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


def _contract(modules: list[dict[str, object]]) -> dict[str, object]:
    return {
        "contract_version": 1,
        "classes": dict(release_manifest.DEPENDENCY_CLASSES),
        "modules": modules,
    }


def _validate(
    contract: dict[str, object],
    *,
    release_root: Path,
    extra_paths: list[Path],
    source_root: Path | None = None,
    shared_root: Path | None = None,
) -> dict[str, object]:
    return validate_release_python_origins(
        contract,
        python_executable=sys.executable,
        release_root=release_root,
        source_root=source_root,
        shared_root=shared_root,
        extra_paths=extra_paths,
    )


def test_correct_release_dependency_passes(tmp_path: Path) -> None:
    release = tmp_path / "releaseA"
    _write_package(release, "pkg_release")
    _write_package(release, "dep_pkg", version="1.0.0")
    contract = _contract(
        [
            {
                "module": "pkg_release",
                "class": "RELEASE_CODE",
                "required": True,
                "allowed_roots": ["."],
            },
            {
                "module": "dep_pkg",
                "class": "RELEASE_DEPENDENCY",
                "required": True,
                "allowed_roots": ["."],
                "version": "1.0.0",
            },
        ]
    )
    result = _validate(contract, release_root=release, extra_paths=[release])
    assert result["ok"] is True, result["errors"]
    assert result["modules"]["dep_pkg"]["version"] == "1.0.0"


def test_missing_required_module_fails(tmp_path: Path) -> None:
    release = tmp_path / "releaseA"
    _write_package(release, "pkg_release")
    contract = _contract(
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


def test_source_tree_leakage_fails(tmp_path: Path) -> None:
    release = tmp_path / "releaseA"
    source = tmp_path / "sourceTree"
    _write_package(release, "pkg_release")
    _write_package(source, "official_pkg")
    contract = _contract(
        [
            {
                "module": "pkg_release",
                "class": "RELEASE_CODE",
                "required": True,
                "allowed_roots": ["."],
            },
            {
                "module": "official_pkg",
                "class": "RELEASE_CODE",
                "required": True,
                "allowed_roots": ["."],
            },
        ]
    )
    result = _validate(
        contract,
        release_root=release,
        extra_paths=[release, source],
        source_root=source,
    )
    assert result["ok"] is False
    assert any(
        error.startswith("SOURCE_TREE_LEAK:official_pkg")
        for error in result["errors"]
    )


def test_shared_runtime_from_source_tree_is_gap(tmp_path: Path) -> None:
    release = tmp_path / "releaseA"
    source = tmp_path / "sourceTree"
    shared = tmp_path / "sharedRuntime"
    _write_package(release, "pkg_release")
    _write_package(source, "shared_layer")
    contract = _contract(
        [
            {
                "module": "shared_layer",
                "class": "SHARED_RUNTIME",
                "required": True,
                "allowed_roots": [str(shared)],
            }
        ]
    )
    result = _validate(
        contract,
        release_root=release,
        extra_paths=[release, source],
        source_root=source,
        shared_root=shared,
    )
    assert result["ok"] is False
    assert any(
        error.startswith("SHARED_RUNTIME_SOURCE_TREE_GAP:shared_layer")
        for error in result["errors"]
    )


def test_versions_are_not_conflated(tmp_path: Path) -> None:
    release_one = tmp_path / "releaseV1"
    release_two = tmp_path / "releaseV2"
    _write_package(release_one, "dep_pkg", version="1.0.0")
    _write_package(release_two, "dep_pkg", version="2.0.0")
    contract_v1 = _contract(
        [
            {
                "module": "dep_pkg",
                "class": "RELEASE_DEPENDENCY",
                "required": True,
                "allowed_roots": ["."],
                "version": "1.0.0",
            }
        ]
    )
    contract_v2 = _contract(
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
    assert _validate(
        contract_v1, release_root=release_one, extra_paths=[release_one]
    )["ok"] is True
    mismatch = _validate(
        contract_v1, release_root=release_two, extra_paths=[release_two]
    )
    assert mismatch["ok"] is False
    assert any(
        error.startswith("DEPENDENCY_VERSION_MISMATCH:dep_pkg")
        for error in mismatch["errors"]
    )
    assert _validate(
        contract_v2, release_root=release_two, extra_paths=[release_two]
    )["ok"] is True


def test_windows_path_resolution(tmp_path: Path) -> None:
    assert normalize_path(str(tmp_path).upper()) == normalize_path(str(tmp_path))
    assert path_is_within(
        tmp_path / "releaseA" / "pkg" / ".." / "pkg" / "mod.py",
        tmp_path / "releaseA",
    )
    assert not path_is_within(
        tmp_path / "releaseA-other", tmp_path / "releaseA"
    )
    if os.name == "nt":
        assert not path_is_within(r"D:\GPTBridge", r"C:\GPTBridge")
    junction = tmp_path / "releaseLink"
    target = tmp_path / "releaseA"
    _write_package(target, "linked_pkg")
    created = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(target)],
        capture_output=True,
        text=True,
    )
    if created.returncode != 0:
        pytest.skip("junction creation unavailable")
    contract = _contract(
        [
            {
                "module": "linked_pkg",
                "class": "RELEASE_CODE",
                "required": True,
                "allowed_roots": ["."],
            }
        ]
    )
    result = _validate(contract, release_root=junction, extra_paths=[junction])
    assert result["ok"] is True, result["errors"]
    assert path_is_within(
        result["modules"]["linked_pkg"]["origin"], target
    )


def test_manifest_matches_actual_dependencies(tmp_path: Path) -> None:
    release = tmp_path / "releaseA"
    _write_package(release, "dep_pkg", version="1.0.0")
    wrong_root_contract = _contract(
        [
            {
                "module": "dep_pkg",
                "class": "RELEASE_DEPENDENCY",
                "required": True,
                "allowed_roots": ["site-packages"],
            }
        ]
    )
    wrong = _validate(
        wrong_root_contract, release_root=release, extra_paths=[release]
    )
    assert wrong["ok"] is False
    assert any(
        error.startswith("ORIGIN_OUTSIDE_ALLOWED_ROOTS:dep_pkg")
        for error in wrong["errors"]
    )

    schema_errors = release_manifest.validate_dependency_contract(
        {"contract_version": 0, "classes": {}, "modules": [{"module": "x"}]}
    )
    assert schema_errors
    shipped = release_manifest.load_dependency_contract()
    assert release_manifest.validate_dependency_contract(shipped) == []
    assert set(shipped["classes"]) == set(release_manifest.DEPENDENCY_CLASSES)


def test_original_backend_and_codex_flow_unaffected(tmp_path: Path) -> None:
    release = tmp_path / "releaseA"
    _write_package(release, "pkg_release")
    contract = _contract(
        [
            {
                "module": "pkg_release",
                "class": "RELEASE_CODE",
                "required": True,
                "allowed_roots": ["."],
            }
        ]
    )
    before_modules = set(sys.modules)
    before_hashes = {
        path.relative_to(release).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in release.rglob("*.py")
    }
    result = _validate(contract, release_root=release, extra_paths=[release])
    assert result["ok"] is True
    after_modules = set(sys.modules)
    assert "pkg_release" not in after_modules - before_modules
    after_hashes = {
        path.relative_to(release).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in release.rglob("*.py")
    }
    assert after_hashes == before_hashes

    probe = probe_python_import_origins(sys.executable, ["numpy"])
    numpy_origin = probe["modules"]["numpy"]["origin"]
    assert numpy_origin and path_is_within(numpy_origin, ROOT / "main-system" / ".venv")

    runtime_result = release_manifest.validate_runtime(
        {
            "minimum_runtime_version": "1.0.0",
            "compatibility_range": {
                "min_runtime": "1.0.0",
                "max_runtime": "2.0.0",
                "read_only_from": "0.9.0",
            },
        },
        runtime_version="1.5.0",
    )
    assert runtime_result == {"compatible": True, "mode": "full", "reason": "ok"}

    from governance_rule.execution.integrity.package_integrity import (
        verify_packaged_app,
    )

    empty = tmp_path / "emptyApp"
    empty.mkdir()
    unchanged = verify_packaged_app(empty)
    assert unchanged["ok"] is False
    assert unchanged["error_code"] == "PACKAGE_UNVERIFIED"
