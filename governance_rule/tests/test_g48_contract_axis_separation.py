"""G48 contract-axis separation + breaking-change tests (synthetic fixtures).

Six release version axes are tracked independently — codex, permission,
ipc, sql_schema, frontend, frontend-backend.  A breaking change on any
single axis must fail closed inside that axis's validator and must not
disturb the other axes (全軸分離); every axis's breaking change must be
detected (破壞性變更測試).  Synthetic fixtures only — the official codex,
permission directory and shipped contracts are never touched.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for src in (ROOT / "shared-layer" / "src", ROOT / "main-system" / "src-core"):
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))

from core_system.contract_validator import ContractRegistry  # noqa: E402
from governance_rule.execution.audit.audit_contract_axes import (  # noqa: E402
    REQUIRED_AXES,
    check_contract_axes,
)
from governance_rule.execution.integrity.python_release_dependencies import (  # noqa: E402
    validate_frontend_release,
    validate_governance_references,
    validate_ipc_contract,
    validate_ipc_surface_pairing,
)

AXES = ("codex", "permission", "ipc", "sql_schema", "frontend", "frontend_backend")

CODEX_VERSION = "2026-09-22T00:00:00Z"
AUTH_VERSION = "2026-09-16T09:22:51Z"
PERMISSION_VERSION = "2026-09-16T05:44:03Z"
SOVEREIGNS = ("decision", "permission")
SQL_SCHEMA_CONSUMER_VERSION = 3

# Error-code families per axis: a break on one axis may only surface that
# axis's codes — anything else would prove the axes are not separated.
AXIS_ERROR_PREFIXES = {
    "codex": ("CODEX_", "SOVEREIGN_", "GOVERNANCE_CONTRACT_"),
    "permission": ("PERMISSION_",),
    "ipc": ("IPC_CONTRACT_",),
    "sql_schema": (),
    "frontend": ("FRONTEND_",),
    "frontend_backend": (
        "IPC_COMMAND_",
        "IPC_EVENT_",
        "IPC_ERROR_CODE_",
        "IPC_FEATURE_",
        "IPC_SURFACE_",
    ),
}


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _synthetic_codex(
    path: Path,
    *,
    codex_version: str = CODEX_VERSION,
    auth_version: str = AUTH_VERSION,
    permission_version: str = PERMISSION_VERSION,
    sovereigns: tuple[str, ...] = SOVEREIGNS,
) -> Path:
    connection = sqlite3.connect(str(path))
    connection.execute("create table metadata (key text, value text)")
    connection.execute(
        "insert into metadata values ('codex_version', ?)", (codex_version,)
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
    connection.execute("create table articles (position integer, provision_id text)")
    connection.commit()
    connection.close()
    return path


def _build_fixture(base: Path) -> dict[str, object]:
    """One synthetic release carrying all six axes, all consistent."""
    repo = base / "repo"
    artifacts = {
        "main": "main-system/dist-ui/main/index.js",
        "preload": "main-system/dist-ui/main/preload.js",
        "renderer": "main-system/dist-ui/renderer/index.html",
    }
    hashes = {}
    for name, relative in artifacts.items():
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"// {name}\n", encoding="utf-8")
        hashes[name] = _sha(path)
    electron_dir = repo / "main-system" / "node_modules" / "electron"
    electron_dir.mkdir(parents=True)
    (electron_dir / "package.json").write_text(
        json.dumps({"version": "39.8.10"}), encoding="utf-8"
    )
    lock = repo / "main-system" / "package-lock.json"
    lock.write_text('{"lockfileVersion": 3}\n', encoding="utf-8")

    config_dir = repo / "main-system" / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    frontend_surface = {
        "surface_version": "fe-v1",
        "backend_commands": ["ping"],
        "events": ["ready"],
        "error_codes": ["E_X"],
        "features": {"streaming": 2},
    }
    backend_surface = {
        "surface_version": "be-v1",
        "requires": {
            "commands": {"ping": {}},
            "events": {"ready": {}},
            "error_codes": ["E_X"],
            "features": {"streaming": 1},
        },
    }
    (config_dir / "ipc-surface-frontend.json").write_text(
        json.dumps(frontend_surface), encoding="utf-8"
    )
    (config_dir / "ipc-surface-backend.json").write_text(
        json.dumps(backend_surface), encoding="utf-8"
    )

    official_ipc = base / "tool-runtime-contract.json"
    official_ipc.write_text(
        json.dumps({"contract_version": 2, "minimum_supported_contract_version": 1}),
        encoding="utf-8",
    )

    sql_dir = base / "config"
    sql_dir.mkdir(exist_ok=True)
    (sql_dir / "sql-schema-contract.json").write_text(
        json.dumps({"contract_version": 3, "minimum_supported_contract_version": 1}),
        encoding="utf-8",
    )

    codex = _synthetic_codex(base / "codex.sqlite3")
    sovereign_identity = hashlib.sha256(
        "\n".join(sorted(SOVEREIGNS)).encode("utf-8")
    ).hexdigest()

    contract = {
        "governance_references": {
            "codex_version": CODEX_VERSION,
            "governance_runtime_contract_version": AUTH_VERSION,
            "permission_contract_version": PERMISSION_VERSION,
            "sovereign_registry_identity": sovereign_identity,
        },
        "ipc_contract": {
            "contract_version": 2,
            "minimum_supported_contract_version": 1,
            "surface_pairing": {
                "frontend_surface_file": "main-system/config/ipc-surface-frontend.json",
                "backend_surface_file": "main-system/config/ipc-surface-backend.json",
                "frontend_surface_version": "fe-v1",
                "backend_surface_version": "be-v1",
            },
        },
        "frontend_release": {
            "electron_version": "39.8.10",
            "artifacts": {"paths": artifacts, "hashes": hashes},
            "dependency_lock": {
                "file": "main-system/package-lock.json",
                "sha256": _sha(lock),
            },
            "ipc_contract_identity": "fe-v1",
            "security": {
                "context_isolation": True,
                "node_integration": False,
                "sandbox": True,
                "channel_allowlist": True,
                "exposes_backend_token_to_renderer": True,
                "token_exposure_policy": "acknowledged-gap-G86",
            },
        },
    }
    return {
        "repo": repo,
        "codex": codex,
        "official_ipc": official_ipc,
        "sql_dir": sql_dir,
        "contract": contract,
    }


def _axis_errors(fixture: dict[str, object]) -> dict[str, list[str]]:
    """Run every axis validator; split governance-references errors by axis."""
    contract = fixture["contract"]
    errors: dict[str, list[str]] = {axis: [] for axis in AXES}
    for error in validate_governance_references(
        contract, codex_path=fixture["codex"]
    ):
        if error.startswith("PERMISSION_"):
            errors["permission"].append(error)
        else:
            errors["codex"].append(error)
    errors["ipc"] = validate_ipc_contract(
        contract, official_contract_path=fixture["official_ipc"]
    )
    errors["frontend_backend"] = validate_ipc_surface_pairing(
        contract, repo_root=fixture["repo"]
    )
    errors["frontend"] = validate_frontend_release(
        contract, repo_root=fixture["repo"]
    )
    registry = ContractRegistry(fixture["sql_dir"])
    sql_errors = registry.validation_errors()
    compat = registry.check_compatibility(
        "sql-schema", SQL_SCHEMA_CONSUMER_VERSION
    )
    if not compat.compatible:
        sql_errors.append(compat.reason)
    errors["sql_schema"] = sql_errors
    return errors


def test_all_axes_pass_when_consistent(tmp_path: Path) -> None:
    fixture = _build_fixture(tmp_path)
    errors = _axis_errors(fixture)
    assert errors == {axis: [] for axis in AXES}, errors


def test_axes_tracked_as_independent_version_fields(tmp_path: Path) -> None:
    """Axis separation at the contract-schema level: six distinct sources."""
    fixture = _build_fixture(tmp_path)
    contract = fixture["contract"]
    refs = contract["governance_references"]
    assert len({refs["codex_version"], refs["permission_contract_version"],
               refs["governance_runtime_contract_version"]}) == 3
    ipc = contract["ipc_contract"]
    assert isinstance(ipc["contract_version"], int)
    assert ipc["contract_version"] != contract["frontend_release"]
    registry = ContractRegistry(fixture["sql_dir"])
    assert registry.contract_version("sql-schema") == 3
    pairing = ipc["surface_pairing"]
    assert pairing["frontend_surface_version"] == "fe-v1"
    assert pairing["backend_surface_version"] == "be-v1"
    assert pairing["frontend_surface_version"] != pairing["backend_surface_version"]
    assert contract["frontend_release"]["ipc_contract_identity"] == "fe-v1"


def test_breaking_change_isolated_to_its_axis(tmp_path: Path) -> None:
    """Each axis's breaking change fails closed in that axis only."""


    def mutate_codex(fixture: dict[str, object]) -> None:
        fixture["contract"]["governance_references"]["codex_version"] = "other"

    def mutate_permission(fixture: dict[str, object]) -> None:
        fixture["contract"]["governance_references"][
            "permission_contract_version"
        ] = "other"

    def mutate_ipc(fixture: dict[str, object]) -> None:
        fixture["contract"]["ipc_contract"]["contract_version"] = 0

    def mutate_sql_schema(fixture: dict[str, object]) -> None:
        (fixture["sql_dir"] / "sql-schema-contract.json").write_text(
            json.dumps(
                {"contract_version": 3, "minimum_supported_contract_version": 5}
            ),
            encoding="utf-8",
        )

    def mutate_frontend(fixture: dict[str, object]) -> None:
        (fixture["repo"] / "main-system/dist-ui/main/preload.js").write_text(
            "// tampered\n", encoding="utf-8"
        )

    def mutate_frontend_backend(fixture: dict[str, object]) -> None:
        surface = fixture["repo"] / "main-system" / "config" / "ipc-surface-frontend.json"
        payload = json.loads(surface.read_text(encoding="utf-8"))
        payload["backend_commands"].append("wipe_disk")
        surface.write_text(json.dumps(payload), encoding="utf-8")

    cases = {
        "codex": mutate_codex,
        "permission": mutate_permission,
        "ipc": mutate_ipc,
        "sql_schema": mutate_sql_schema,
        "frontend": mutate_frontend,
        "frontend_backend": mutate_frontend_backend,
    }
    for axis, mutate in cases.items():
        fixture = _build_fixture(tmp_path / axis)
        mutate(fixture)
        errors = _axis_errors(fixture)
        assert errors[axis], f"{axis}: breaking change not detected: {errors}"
        for error in errors[axis]:
            prefixes = AXIS_ERROR_PREFIXES[axis]
            if prefixes:
                assert error.startswith(prefixes), (
                    f"{axis}: foreign error code {error}"
                )
        for other in AXES:
            if other != axis:
                assert errors[other] == [], (
                    f"{axis} break leaked into {other}: {errors[other]}"
                )


def test_codex_axis_breaking_variants(tmp_path: Path) -> None:
    fixture = _build_fixture(tmp_path)
    fixture["contract"]["governance_references"]["codex_version"] = "other"
    errors = _axis_errors(fixture)
    assert any(e.startswith("CODEX_VERSION_MISMATCH") for e in errors["codex"])

    fixture = _build_fixture(tmp_path / "v2")
    fixture["contract"]["governance_references"][
        "governance_runtime_contract_version"
    ] = "other"
    errors = _axis_errors(fixture)
    assert any(
        e.startswith("GOVERNANCE_CONTRACT_INCOMPATIBLE") for e in errors["codex"]
    )


def test_ipc_axis_both_directions_rejected(tmp_path: Path) -> None:
    fixture = _build_fixture(tmp_path)
    fixture["contract"]["ipc_contract"]["contract_version"] = 0
    fixture["contract"]["ipc_contract"]["minimum_supported_contract_version"] = 0
    errors = _axis_errors(fixture)
    assert any("release-too-old" in e for e in errors["ipc"])

    fixture = _build_fixture(tmp_path / "v2")
    fixture["contract"]["ipc_contract"]["minimum_supported_contract_version"] = 9
    errors = _axis_errors(fixture)
    assert any("release-too-new" in e for e in errors["ipc"])


def test_sql_schema_axis_consumer_window(tmp_path: Path) -> None:
    fixture = _build_fixture(tmp_path)
    registry = ContractRegistry(fixture["sql_dir"])
    assert registry.check_compatibility("sql-schema", 3).compatible
    assert registry.check_compatibility("sql-schema", 1).compatible
    assert not registry.check_compatibility("sql-schema", 0).compatible
    assert not registry.check_compatibility("sql-schema", 4).compatible

    (fixture["sql_dir"] / "sql-schema-contract.json").write_text(
        json.dumps({"contract_version": 2, "minimum_supported_contract_version": 3}),
        encoding="utf-8",
    )
    registry = ContractRegistry(fixture["sql_dir"])
    assert any(
        "minimum-exceeds-version" in e for e in registry.validation_errors()
    )


def test_real_repo_all_axes_present_and_valid() -> None:
    errors: list[str] = []
    check_contract_axes(ROOT, errors)
    assert errors == []
    registry = ContractRegistry(ROOT / "main-system" / "config")
    validations = registry.validate_all()
    for axis in REQUIRED_AXES:
        validation = validations.get(axis)
        assert validation is not None, f"axis missing: {axis}"
        assert validation.ok, f"axis invalid: {axis}:{validation.errors}"
