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


__all__ = [
    "load_manifest",
    "get_manifest_path",
    "validate_runtime",
    "get_compatibility_matrix",
]
