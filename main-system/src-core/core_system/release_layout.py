"""Release bundle layout — 開發／正式環境分離 (A576; §10.19).

Contract verifier for the isolated release payload that a certified update
runs from::

    runtime/releases/<release_id>/
    ├─ backend/          # Backend code
    ├─ dependencies/     # Shared runtime dependencies
    └─ manifest.json     # Required contracts + dependency lock + build metadata

The physical build (rebuild the venv in the final isolation location, bind
``uv.lock`` identity, gate with ``validate_release_bundle``) is W4-0c; this
module provides the *contract* that build must satisfy and that the update
path re-checks before a standby backend is allowed to run from a bundle.

§10.19 rules enforced here:

* **Release 必須包含** backend code、shared runtime dependencies、required
  contracts、dependency lock、build metadata —— 缺一即 fail-closed。
* **不得隨更新複製** PostgreSQL 資料、Qdrant 索引、模型權重、使用者資料 ——
  任一隨 payload 複製即拒絕（這些是正式權威狀態，存在於 release 之外）。

The five compatibility gates (codex／permissions／ipc／sql_schema／frontend)
are **not** re-implemented here; they belong to
:mod:`core_system.active_release_verify` (and the governance
``validate_release_bundle`` gate).  Callers that hold a dependency contract
may pass ``gate_fn`` so the W4-0c builder reuses that existing gate instead of
building a second one.
"""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Callable, Final, Iterable

SCHEMA_VERSION: Final = "star-release-layout/v1"

#: Required top-level members of ``runtime/releases/<release_id>/`` (§10.19 +
#: W4-0c layout).
REQUIRED_ENTRIES: Final[tuple[str, ...]] = (
    "backend",
    "dependencies",
    "manifest.json",
)

#: Manifest members that prove the remaining §10.19 required content.
MANIFEST_REQUIRED: Final[tuple[str, ...]] = (
    "release_id",
    "required_contracts",
    "dependency_lock",
    "build_metadata",
)

#: 不得隨更新複製 —— file-name / directory rules → violation reason code.
_FORBIDDEN_NAMES: Final[dict[str, str]] = {
    "pgdata": "postgres-data",
    "postgres": "postgres-data",
    "postmaster.pid": "postgres-data",
    "global": "postgres-data",
    "base": "postgres-data",
    "qdrant_storage": "qdrant-index",
    "qdrant": "qdrant-index",
    "user-data": "user-data",
    "user_data": "user-data",
    "users": "user-data",
}

_FORBIDDEN_SUFFIXES: Final[dict[str, str]] = {
    ".pt": "model-weights",
    ".pth": "model-weights",
    ".ckpt": "model-weights",
    ".safetensors": "model-weights",
    ".gguf": "model-weights",
    ".onnx": "model-weights",
    ".h5": "model-weights",
    ".pgdump": "postgres-data",
    ".dump": "postgres-data",
    ".qdrant": "qdrant-index",
}

# Bound the sweep so a runaway tree can never turn the check into a scan of
# the whole disk; release payloads are shallow by construction.
_MAX_DEPTH: Final = 8
_MAX_ENTRIES: Final = 200_000


def _iter_entries(root: Path) -> list[os.DirEntry[str]]:
    """Bounded, symlink-safe shallow-first sweep of the payload."""
    found: list[os.DirEntry[str]] = []
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack:
        current, depth = stack.pop()
        if depth > _MAX_DEPTH:
            continue
        try:
            with os.scandir(current) as it:
                for entry in it:
                    found.append(entry)
                    if len(found) >= _MAX_ENTRIES:
                        return found
                    if entry.is_dir(follow_symlinks=False):
                        stack.append((Path(entry.path), depth + 1))
        except OSError:
            continue
    return found


def _forbidden_violations(root: Path) -> list[str]:
    violations: list[str] = []
    for entry in _iter_entries(root):
        name = entry.name
        lower = name.lower()
        reason = _FORBIDDEN_NAMES.get(lower)
        if reason is None:
            suffix = Path(lower).suffix
            reason = _FORBIDDEN_SUFFIXES.get(suffix)
        if reason is None:
            continue
        try:
            relative = Path(entry.path).relative_to(root).as_posix()
        except ValueError:
            relative = entry.path
        violations.append(f"{relative}:{reason}")
    return sorted(violations)


def _manifest_errors(manifest: dict[str, Any] | None) -> list[str]:
    if manifest is None:
        return []
    errors: list[str] = []
    for field in MANIFEST_REQUIRED:
        if field not in manifest:
            errors.append(f"MANIFEST_MISSING:{field}")
    if not manifest.get("release_id"):
        errors.append("MANIFEST_MISSING:release_id")
    if not manifest.get("required_contracts"):
        errors.append("MANIFEST_MISSING:required_contracts")
    lock = manifest.get("dependency_lock")
    if lock is not None and not (
        isinstance(lock, dict) and lock.get("identity")
    ):
        errors.append("MANIFEST_MISSING:dependency_lock.identity")
    if "build_metadata" in manifest and not manifest.get("build_metadata"):
        errors.append("MANIFEST_MISSING:build_metadata")
    return errors


def check_release_layout(
    release_root: str | os.PathLike[str],
    *,
    manifest: dict[str, Any] | None = None,
    gate_fn: Callable[[dict[str, Any]], list[str]] | None = None,
) -> dict[str, Any]:
    """Verify a ``runtime/releases/<release_id>/`` payload against §10.19.

    ``manifest`` may be supplied to avoid a second read; when omitted and
    ``manifest.json`` exists it is loaded and checked.  ``gate_fn`` is an
    optional reuse hook: it receives the dependency contract and returns
    error strings (e.g. the governance ``validate_release_bundle`` adapter);
    callers must not re-implement the five compatibility gates here.
    """
    root = Path(os.fspath(release_root))
    errors: list[str] = []

    if not root.is_dir():
        return {
            "schema_version": SCHEMA_VERSION,
            "ok": False,
            "release_root": str(root),
            "missing": list(REQUIRED_ENTRIES),
            "forbidden": [],
            "manifest_errors": [],
            "gate_errors": [],
            "reason": "release-root-missing",
        }

    missing = [name for name in REQUIRED_ENTRIES if not (root / name).exists()]
    errors.extend(f"REQUIRED_MISSING:{name}" for name in missing)

    forbidden = _forbidden_violations(root)
    errors.extend(f"FORBIDDEN_PAYLOAD:{item}" for item in forbidden)

    if manifest is None:
        manifest_path = root / "manifest.json"
        if manifest_path.is_file():
            import json

            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as error:
                manifest = None
                errors.append(f"MANIFEST_UNREADABLE:{error}")
    manifest_errors = _manifest_errors(manifest)
    errors.extend(manifest_errors)

    gate_errors: list[str] = []
    if gate_fn is not None and isinstance(manifest, dict):
        contract = manifest.get("dependency_contract")
        if isinstance(contract, dict):
            try:
                gate_errors = [str(e) for e in gate_fn(contract)]
            except Exception as error:  # fail-closed: a broken gate rejects
                gate_errors = [f"GATE_FAILED:{error}"]
            errors.extend(gate_errors)

    deduped = list(dict.fromkeys(errors))
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": not deduped,
        "release_root": str(root),
        "missing": missing,
        "forbidden": forbidden,
        "manifest_errors": manifest_errors,
        "gate_errors": gate_errors,
        "reason": "" if not deduped else "release-layout-invalid",
    }


__all__ = [
    "SCHEMA_VERSION",
    "REQUIRED_ENTRIES",
    "MANIFEST_REQUIRED",
    "check_release_layout",
]
