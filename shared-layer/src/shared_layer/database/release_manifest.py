"""Database Release Manifest (F2).

Loads and validates the database-release.json manifest at startup.
Binds PostgreSQL schema, SQLite template, RLS, Role, Migration,
Qdrant collection, query contract, reconcile contract, backup format,
and minimum runtime version into a single Database Release.

Usage:
    from shared_layer.database.release_manifest import load_manifest, validate_runtime

    manifest = load_manifest()
    validate_runtime(manifest, runtime_version="1.0.0")

Codex basis:
    A8/E21  — PostgreSQL: central-structured-official-data.
    A10/E10 — explicit-allowlist; deny-by-default.
    A46/E22 — Audit: mandatory-ledger.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_MANIFEST_PATH = Path(__file__).resolve().parents[3] / "database-release.json"
_DEPENDENCY_CONTRACT_PATH = Path(__file__).resolve().parents[3] / "release-dependencies.json"

# Release dependency classes.  A release manifest classifies every artifact
# it carries or consumes; the class decides the allowed origin and the
# validation rule (release isolation contract).
DEPENDENCY_CLASSES: dict[str, str] = {
    "RELEASE_CODE": "fixed by the release; origin must resolve inside the release root",
    "RELEASE_DEPENDENCY": "verifiable version and source; never resolved from the development source tree",
    "SHARED_RUNTIME": "explicit shared contract and ownership; only the declared shared root",
    "PERSISTENT_DATA": "never overwritten by program updates; not importable",
    "SECRET": "never packaged into the release; not importable",
    "DEVELOPMENT_ONLY": "never loaded at production runtime",
}


def load_manifest() -> dict[str, Any]:
    """Load the database-release.json manifest."""
    with open(_MANIFEST_PATH, encoding="utf-8") as f:
        return json.load(f)


def get_manifest_path() -> Path:
    """Get the path to the database-release.json manifest."""
    return _MANIFEST_PATH


def validate_runtime(
    manifest: dict[str, Any],
    *,
    runtime_version: str,
) -> dict[str, Any]:
    """Validate the runtime version against the manifest.

    Returns a dict with:
      - 'compatible': bool
      - 'mode': 'full' | 'read-only' | 'rejected'
      - 'reason': str
    """
    min_runtime = manifest.get("minimum_runtime_version", "0.0.0")
    compat = manifest.get("compatibility_range", {})
    min_full = compat.get("min_runtime", min_runtime)
    max_full = compat.get("max_runtime", "999.0.0")
    read_only_from = compat.get("read_only_from", "0.0.0")

    if _version_lt(runtime_version, read_only_from):
        return {"compatible": False, "mode": "rejected",
                "reason": f"runtime {runtime_version} < read_only_from {read_only_from}"}

    if _version_lt(runtime_version, min_full):
        return {"compatible": True, "mode": "read-only",
                "reason": f"runtime {runtime_version} < min_full {min_full}"}

    if _version_gt(runtime_version, max_full):
        return {"compatible": True, "mode": "read-only",
                "reason": f"runtime {runtime_version} > max_full {max_full}"}

    return {"compatible": True, "mode": "full", "reason": "ok"}


def _version_lt(a: str, b: str) -> bool:
    """Compare two version strings (semver-like)."""
    parts_a = [int(x) for x in a.split(".") if x.isdigit()]
    parts_b = [int(x) for x in b.split(".") if x.isdigit()]
    return parts_a < parts_b


def _version_gt(a: str, b: str) -> bool:
    """Compare two version strings (semver-like)."""
    parts_a = [int(x) for x in a.split(".") if x.isdigit()]
    parts_b = [int(x) for x in b.split(".") if x.isdigit()]
    return parts_a > parts_b


def get_compatibility_matrix(manifest: dict[str, Any]) -> dict[str, Any]:
    """Get the compatibility range from the manifest."""
    return manifest.get("compatibility_range", {})


def load_dependency_contract(path: Path | None = None) -> dict[str, Any]:
    """Load the release dependency contract (classes + module origins)."""
    contract_path = Path(path) if path else _DEPENDENCY_CONTRACT_PATH
    with open(contract_path, encoding="utf-8") as stream:
        payload = json.load(stream)
    if not isinstance(payload, dict):
        raise ValueError("release dependency contract must be an object")
    return payload


def get_dependency_contract_path() -> Path:
    """Get the path to the release dependency contract."""
    return _DEPENDENCY_CONTRACT_PATH


def validate_dependency_contract(contract: dict[str, Any]) -> list[str]:
    """Schema-level validation of the release dependency contract."""
    errors: list[str] = []
    if type(contract.get("contract_version")) is not int or contract["contract_version"] < 1:
        errors.append("contract_version must be a positive integer")
    classes = contract.get("classes")
    if not isinstance(classes, dict) or set(classes) != set(DEPENDENCY_CLASSES):
        errors.append("classes must declare exactly the six release dependency classes")
    modules = contract.get("modules")
    if not isinstance(modules, list):
        errors.append("modules must be a list")
        return errors
    seen: set[str] = set()
    for index, entry in enumerate(modules):
        if not isinstance(entry, dict):
            errors.append(f"modules[{index}] must be an object")
            continue
        name = entry.get("module")
        if not isinstance(name, str) or not name:
            errors.append(f"modules[{index}].module is required")
            continue
        if name in seen:
            errors.append(f"duplicate module entry: {name}")
        seen.add(name)
        dependency_class = entry.get("class")
        if dependency_class not in DEPENDENCY_CLASSES:
            errors.append(f"{name}: unknown dependency class {dependency_class!r}")
        if dependency_class in {"PERSISTENT_DATA", "SECRET"}:
            errors.append(f"{name}: class {dependency_class} cannot be an import module")
        if dependency_class == "SHARED_RUNTIME" and not entry.get("allowed_roots"):
            errors.append(f"{name}: SHARED_RUNTIME requires allowed_roots")
    for secret in contract.get("secrets") or []:
        if not isinstance(secret, str) or not secret:
            errors.append("secrets entries must be non-empty strings")
    return errors


def validate_python_release_dependencies(
    contract: dict[str, Any],
    *,
    python_executable: str,
    release_root: str,
    source_root: str | None = None,
    shared_root: str | None = None,
    extra_paths: tuple[str, ...] = (),
    cwd: str | None = None,
) -> dict[str, Any]:
    """Probe the release interpreter and validate module origins.

    Delegates to ``governance_rule.execution.integrity.python_release_dependencies``
    (lazy import keeps the shared-layer import surface unchanged for callers
    that only read the database release manifest).
    """
    errors = validate_dependency_contract(contract)
    if errors:
        return {"ok": False, "errors": errors, "modules": {}, "probe": {}}
    from governance_rule.execution.integrity.python_release_dependencies import (
        validate_release_python_origins,
    )

    return validate_release_python_origins(
        contract,
        python_executable=python_executable,
        release_root=release_root,
        source_root=source_root,
        shared_root=shared_root,
        extra_paths=extra_paths,
        cwd=cwd,
    )


def validate_release_bundle(
    contract: dict[str, Any],
    *,
    python_executable: str,
    release_root: str,
    source_root: str | None = None,
    shared_root: str | None = None,
    extra_paths: tuple[str, ...] = (),
    cwd: str | None = None,
    codex_path: str | None = None,
    allowed_dependency_roots: tuple[str, ...] = (),
    service_code_roots: tuple[str, ...] = (),
    check_forbidden_content: bool = False,
    official_contract_path: str | None = None,
    official_root: str | None = None,
    check_official_state_separation: bool = False,
    repo_root: str | None = None,
    ipc_frontend_surface_path: str | None = None,
    ipc_backend_surface_path: str | None = None,
    check_frontend_release: bool = False,
    check_persistent_data_separation: bool = False,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Full release-bundle validation (environment, lock, origins, natives).

    Governance references are checked when ``codex_path`` is given; the
    check reads the official codex read-only and is **not** a substitute for
    the governed codex validation/authorization flow.
    """
    errors = validate_dependency_contract(contract)
    if errors:
        return {
            "ok": False,
            "errors": errors,
            "environment": {},
            "origins": {},
            "lock": {},
        }
    from governance_rule.execution.integrity.python_release_dependencies import (
        validate_release_bundle as _validate_bundle,
    )

    return _validate_bundle(
        contract,
        python_executable=python_executable,
        release_root=release_root,
        source_root=source_root,
        shared_root=shared_root,
        extra_paths=extra_paths,
        cwd=cwd,
        codex_path=codex_path,
        allowed_dependency_roots=allowed_dependency_roots,
        service_code_roots=service_code_roots,
        check_forbidden_content=check_forbidden_content,
        official_contract_path=official_contract_path,
        official_root=official_root,
        check_official_state_separation=check_official_state_separation,
        repo_root=repo_root,
        ipc_frontend_surface_path=ipc_frontend_surface_path,
        ipc_backend_surface_path=ipc_backend_surface_path,
        check_frontend_release=check_frontend_release,
        check_persistent_data_separation=check_persistent_data_separation,
        env=env,
    )


def validate_secret_exclusions(
    contract: dict[str, Any],
    release_paths: list[str],
) -> list[str]:
    """SECRET entries must never exist inside the release payload."""
    errors: list[str] = []
    lowered = [path.replace("\\", "/").lower() for path in release_paths]
    for secret in contract.get("secrets") or []:
        pattern = str(secret).replace("\\", "/").lower()
        suffix = pattern.lstrip("*")
        for path in lowered:
            if suffix and path.endswith(suffix):
                errors.append(f"SECRET_PACKAGED:{secret}:{path}")
    return errors


__all__ = [
    "DEPENDENCY_CLASSES",
    "load_manifest",
    "get_manifest_path",
    "validate_runtime",
    "get_compatibility_matrix",
    "load_dependency_contract",
    "get_dependency_contract_path",
    "validate_dependency_contract",
    "validate_python_release_dependencies",
    "validate_release_bundle",
    "validate_secret_exclusions",
]
