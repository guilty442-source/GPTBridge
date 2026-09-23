"""Release compatibility-gate tests (isolated synthetic releases only).

Covers: shared-layer contract version pass/incompatible, governance and
permission contract incompatibility, codex integrity rejection, missing
governance dependencies, source-tree leakage, service/authority
duplication gates, cross-release code sharing, official-state packaging,
IPC contract compatibility, and the untouched original backend.  The
official codex and permission state are never modified; synthetic codex
databases live in temporary directories.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SHARED_SRC = ROOT / "shared-layer" / "src"
if str(SHARED_SRC) not in sys.path:
    sys.path.insert(0, str(SHARED_SRC))

from governance_rule.execution.codex_postgresql import authority_state  # noqa: E402
from governance_rule.execution.integrity.python_release_dependencies import (  # noqa: E402
    validate_forbidden_release_content,
    validate_governance_references,
    validate_ipc_contract,
    validate_official_state_separation,
    validate_release_bundle,
)
from shared_layer.database import release_manifest  # noqa: E402

VENV_ROOT = Path(sys.executable).resolve().parents[1]


def _write_package(root: Path, name: str) -> Path:
    package = root / name
    package.mkdir(parents=True, exist_ok=True)
    (package / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
    return package


def _contract(modules: list[dict[str, object]]) -> dict[str, object]:
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
        "service_topology": {
            "shared_layer_service": {
                "mode": "connect-to-existing",
                "duplicate_instance_allowed": False,
            },
            "governance_authority": {
                "mode": "reference-existing",
                "duplicate_authority_allowed": False,
            },
        },
    }


def _validate(
    contract: dict[str, object],
    *,
    release_root: Path,
    extra_paths: list[Path],
    source_root: Path | None = None,
    codex_path: Path | None = None,
) -> dict[str, object]:
    return validate_release_bundle(
        contract,
        python_executable=sys.executable,
        release_root=release_root,
        source_root=source_root,
        extra_paths=extra_paths,
        allowed_dependency_roots=[VENV_ROOT],
        codex_path=codex_path,
    )


def _synthetic_codex(
    path: Path,
    *,
    version: str = "2026-09-20T17:39:12Z",
    auth_version: str = "2026-09-16T09:22:51Z",
    permission_version: str = "2026-09-16T05:44:03Z",
    sovereigns: tuple[str, ...] = ("decision", "permission"),
    include_articles: bool = True,
) -> Path:
    connection = sqlite3.connect(str(path))
    connection.execute("create table metadata (key text, value text)")
    connection.execute(
        "insert into metadata values ('codex_version', ?)", (version,)
    )
    connection.execute(
        "create table identity_authentication_contract (version_identity text)"
    )
    connection.execute(
        "insert into identity_authentication_contract values (?)", (auth_version,)
    )
    connection.execute(
        "create table sql_session_binding_contract (version_identity text)"
    )
    connection.execute(
        "insert into sql_session_binding_contract values (?)", (permission_version,)
    )
    connection.execute("create table sovereigns (sovereign_id text)")
    for sovereign in sovereigns:
        connection.execute("insert into sovereigns values (?)", (sovereign,))
    if include_articles:
        connection.execute("create table articles (position integer, provision_id text)")
    connection.commit()
    connection.close()
    return path


def test_shared_layer_contract_version_passes(tmp_path: Path) -> None:
    release = tmp_path / "releaseA"
    module = _write_package(release, "shl_contract")
    (module / "__init__.py").write_text("VERSION = 1\n", encoding="utf-8")
    contract = _contract([])
    contract["shared_layer_contract"] = {
        "version_key": "shl_contract:VERSION",
        "version": 1,
        "compatible_versions": [1],
    }
    result = _validate(contract, release_root=release, extra_paths=[release])
    assert result["ok"] is True, result["errors"]


def test_shared_layer_contract_incompatible_fails(tmp_path: Path) -> None:
    release = tmp_path / "releaseA"
    module = _write_package(release, "shl_contract")
    (module / "__init__.py").write_text("VERSION = 1\n", encoding="utf-8")
    contract = _contract([])
    contract["shared_layer_contract"] = {
        "version_key": "shl_contract:VERSION",
        "version": 2,
        "compatible_versions": [2],
    }
    result = _validate(contract, release_root=release, extra_paths=[release])
    assert result["ok"] is False
    assert any(
        error.startswith("SHARED_LAYER_CONTRACT_INCOMPATIBLE")
        for error in result["errors"]
    )
    contract["shared_layer_contract"] = {
        "version_key": "shl_contract:MISSING",
        "version": 1,
    }
    unreadable = _validate(contract, release_root=release, extra_paths=[release])
    assert any(
        error.startswith("SHARED_LAYER_CONTRACT_UNREADABLE")
        for error in unreadable["errors"]
    )


def test_governance_contract_incompatible_fails(tmp_path: Path) -> None:
    release = tmp_path / "releaseA"
    _write_package(release, "pkg_release")
    codex = _synthetic_codex(tmp_path / "codex.sqlite3")
    contract = _contract([])
    contract["governance_references"] = {
        "governance_runtime_contract_version": "2026-01-01T00:00:00Z",
        "permission_contract_version": "2026-09-16T05:44:03Z",
    }
    result = _validate(
        contract, release_root=release, extra_paths=[release], codex_path=codex
    )
    assert result["ok"] is False
    assert any(
        error.startswith("GOVERNANCE_CONTRACT_INCOMPATIBLE")
        for error in result["errors"]
    )
    contract["governance_references"] = {
        "governance_runtime_contract_version": "2026-09-16T09:22:51Z",
        "permission_contract_version": "2026-01-01T00:00:00Z",
    }
    permission = _validate(
        contract, release_root=release, extra_paths=[release], codex_path=codex
    )
    assert any(
        error.startswith("PERMISSION_CONTRACT_INCOMPATIBLE")
        for error in permission["errors"]
    )
    contract["governance_references"] = {
        "governance_runtime_contract_version": "2026-09-16T09:22:51Z",
        "permission_contract_version": "2026-09-16T05:44:03Z",
    }
    assert (
        _validate(
            contract, release_root=release, extra_paths=[release], codex_path=codex
        )["ok"]
        is True
    )


def test_codex_integrity_failure_rejected(tmp_path: Path) -> None:
    release = tmp_path / "releaseA"
    _write_package(release, "pkg_release")
    codex = _synthetic_codex(tmp_path / "broken.sqlite3", include_articles=False)
    contract = _contract([])
    contract["governance_references"] = {}
    result = _validate(
        contract, release_root=release, extra_paths=[release], codex_path=codex
    )
    assert result["ok"] is False
    assert any(
        error.startswith("CODEX_INTEGRITY_FAILED") for error in result["errors"]
    )


def test_missing_governance_dependency_rejected(tmp_path: Path) -> None:
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
    contract["governance_dependencies"] = [
        {"dependency": "absent_governance_module.execution.audit", "kind": "module", "required": True},
        {"dependency": "missing/audit", "kind": "path", "required": True},
    ]
    result = _validate(contract, release_root=release, extra_paths=[release])
    assert result["ok"] is False
    missing = [
        error
        for error in result["errors"]
        if error.startswith("GOVERNANCE_DEPENDENCY_MISSING")
    ]
    assert any("missing/audit" in error for error in missing)
    assert any(
        "absent_governance_module.execution.audit" in error for error in missing
    )


def test_source_tree_import_leakage_detected(tmp_path: Path) -> None:
    release = tmp_path / "releaseA"
    source = tmp_path / "sourceTree"
    _write_package(release, "pkg_release")
    _write_package(source, "governance_rule")
    contract = _contract(
        [
            {
                "module": "governance_rule",
                "class": "RELEASE_CODE",
                "required": True,
                "allowed_roots": ["."],
            }
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
        error.startswith("SOURCE_TREE_LEAK:governance_rule")
        for error in result["errors"]
    )


def test_no_second_shared_layer_service(tmp_path: Path) -> None:
    release = tmp_path / "releaseA"
    _write_package(release, "pkg_release")
    contract = _contract([])
    contract["service_topology"]["shared_layer_service"] = {
        "mode": "connect-to-existing",
        "duplicate_instance_allowed": True,
    }
    result = _validate(contract, release_root=release, extra_paths=[release])
    assert any(
        error == "SHARED_SERVICE_DUPLICATION_FORBIDDEN"
        for error in result["errors"]
    )
    contract["service_topology"]["shared_layer_service"] = {
        "mode": "start-own-instance",
        "duplicate_instance_allowed": False,
    }
    result = _validate(contract, release_root=release, extra_paths=[release])
    assert any(
        error.startswith("SHARED_LAYER_SERVICE_MODE_INVALID")
        for error in result["errors"]
    )


def test_no_second_governance_authority(tmp_path: Path) -> None:
    release = tmp_path / "releaseA"
    _write_package(release, "pkg_release")
    contract = _contract([])
    contract["service_topology"]["governance_authority"] = {
        "mode": "reference-existing",
        "duplicate_authority_allowed": True,
    }
    result = _validate(contract, release_root=release, extra_paths=[release])
    assert any(
        error == "GOVERNANCE_AUTHORITY_DUPLICATION_FORBIDDEN"
        for error in result["errors"]
    )
    contract["service_topology"]["governance_authority"] = {
        "mode": "own-authority",
        "duplicate_authority_allowed": False,
    }
    result = _validate(contract, release_root=release, extra_paths=[release])
    assert any(
        error.startswith("GOVERNANCE_AUTHORITY_MODE_INVALID")
        for error in result["errors"]
    )


def test_releases_do_not_share_mutable_code(tmp_path: Path) -> None:
    release_a = tmp_path / "releaseA"
    release_b = tmp_path / "releaseB"
    _write_package(release_a, "shared_name")
    _write_package(release_b, "shared_name")
    contract = _contract(
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
        contract, release_root=release_b, extra_paths=[release_a, release_b]
    )
    assert result["ok"] is False
    assert any(
        error.startswith("ORIGIN_OUTSIDE_ALLOWED_ROOTS:shared_name")
        for error in result["errors"]
    )


def test_official_state_not_packaged(tmp_path: Path) -> None:
    release = tmp_path / "releaseA"
    _write_package(release, "pkg_release")
    official_root = tmp_path / "workspace"
    official_root.mkdir()
    copied = official_root / "governance_rule" / "codex" / "data"
    copied.mkdir(parents=True)
    (copied / "governance_codex.sqlite3").write_bytes(b"db")
    contract = _contract([])
    contract["official_state_paths"] = [
        "governance_rule/codex/data/governance_codex.sqlite3"
    ]
    clean = validate_official_state_separation(
        contract, release_root=release, official_root=official_root
    )
    assert clean == []
    inside = validate_official_state_separation(
        contract, release_root=official_root, official_root=official_root
    )
    assert any(
        error.startswith("OFFICIAL_STATE_INSIDE_RELEASE") for error in inside
    )
    contract["forbidden_content"] = ["governance_codex.sqlite3"]
    packaged = validate_forbidden_release_content(contract, official_root)
    assert packaged["ok"] is False


def test_ipc_contract_compatibility(tmp_path: Path) -> None:
    official = tmp_path / "tool-runtime-contract.json"
    official.write_text(
        json.dumps(
            {
                "contract_version": 2,
                "minimum_supported_contract_version": 1,
            }
        ),
        encoding="utf-8",
    )
    compatible = {
        "ipc_contract": {
            "contract_version": 1,
            "minimum_supported_contract_version": 1,
        }
    }
    assert validate_ipc_contract(
        compatible, official_contract_path=official
    ) == []
    too_new = {
        "ipc_contract": {
            "contract_version": 3,
            "minimum_supported_contract_version": 3,
        }
    }
    errors = validate_ipc_contract(too_new, official_contract_path=official)
    assert any(error.startswith("IPC_CONTRACT_INCOMPATIBLE") for error in errors)
    official.write_text(
        json.dumps(
            {
                "contract_version": 2,
                "minimum_supported_contract_version": 2,
            }
        ),
        encoding="utf-8",
    )
    too_old = {
        "ipc_contract": {
            "contract_version": 1,
            "minimum_supported_contract_version": 1,
        }
    }
    errors = validate_ipc_contract(too_old, official_contract_path=official)
    assert any("release-too-old" in error for error in errors)


def test_original_backend_unaffected(tmp_path: Path) -> None:
    pyvenv = VENV_ROOT / "pyvenv.cfg"
    before_pyvenv = hashlib.sha256(pyvenv.read_bytes()).hexdigest()
    before_codex = str(authority_state().get("source_sha256") or "")
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
    result = _validate(contract, release_root=release, extra_paths=[release])
    assert result["ok"] is True
    shipped = release_manifest.load_dependency_contract()
    assert validate_governance_references(shipped) == []
    assert hashlib.sha256(pyvenv.read_bytes()).hexdigest() == before_pyvenv
    assert str(authority_state().get("source_sha256") or "") == before_codex
