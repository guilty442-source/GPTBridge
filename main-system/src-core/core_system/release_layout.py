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
    "payload_snapshot",
)

PAYLOAD_SNAPSHOT_SCHEMA: Final = "star-release-payload/v1"
REQUIRED_PAYLOAD_ROOTS: Final[tuple[str, ...]] = ("backend", "dependencies")
_MAX_PAYLOAD_MISMATCHES: Final = 20

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


def _is_sha256_digest(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and value == value.lower()
        and all(character in "0123456789abcdef" for character in value)
    )


def _valid_relative_path(value: Any, *, allow_root: bool = False) -> bool:
    if not isinstance(value, str) or not value or "\\" in value or "\0" in value:
        return False
    if PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute():
        return False
    candidate = PurePosixPath(value)
    if candidate.as_posix() != value:
        return False
    if value == ".":
        return allow_root
    return ".." not in candidate.parts and not any(":" in part for part in candidate.parts)


def _payload_root_covers(root: str, relative_path: str) -> bool:
    return root == "." or relative_path == root or relative_path.startswith(f"{root}/")


def validate_payload_snapshot(
    snapshot: Any,
    *,
    required_roots: Iterable[str] = REQUIRED_PAYLOAD_ROOTS,
) -> list[str]:
    """Validate the immutable payload snapshot recorded in a release manifest.

    The snapshot is generated by :func:`build_release_payload_snapshot` from
    ``governance_rule.execution.integrity.package_integrity`` so a release can
    detect in-place payload changes such as ``pip install/upgrade``.
    """
    if not isinstance(snapshot, dict):
        return ["PAYLOAD_SNAPSHOT_INVALID:not-an-object"]

    errors: list[str] = []
    if snapshot.get("schema_version") != PAYLOAD_SNAPSHOT_SCHEMA:
        errors.append("PAYLOAD_SNAPSHOT_INVALID:schema_version")
    if snapshot.get("algorithm") != "sha256":
        errors.append("PAYLOAD_SNAPSHOT_INVALID:algorithm")

    roots = snapshot.get("roots")
    normalized_roots: tuple[str, ...] = ()
    if (
        not isinstance(roots, list)
        or not roots
        or len(roots) != len(set(roots))
        or not all(_valid_relative_path(root, allow_root=True) for root in roots)
    ):
        errors.append("PAYLOAD_SNAPSHOT_INVALID:roots")
    else:
        normalized_roots = tuple(roots)
        if "." not in normalized_roots:
            missing_roots = [
                root for root in required_roots if root not in normalized_roots
            ]
            errors.extend(
                f"PAYLOAD_SNAPSHOT_ROOT_MISSING:{root}" for root in missing_roots
            )

    files = snapshot.get("files")
    normalized_files: dict[str, str] = {}
    if not isinstance(files, dict) or not files:
        errors.append("PAYLOAD_SNAPSHOT_INVALID:files")
    else:
        for relative_path, digest in files.items():
            if not _valid_relative_path(relative_path):
                errors.append(f"PAYLOAD_SNAPSHOT_INVALID:file:{relative_path}")
                continue
            if not _is_sha256_digest(digest):
                errors.append(f"PAYLOAD_SNAPSHOT_INVALID:digest:{relative_path}")
                continue
            if normalized_roots and not any(
                _payload_root_covers(root, relative_path)
                for root in normalized_roots
            ):
                errors.append(f"PAYLOAD_SNAPSHOT_FILE_OUTSIDE_ROOT:{relative_path}")
                continue
            normalized_files[relative_path] = digest

    file_count = snapshot.get("file_count")
    if (
        type(file_count) is not int
        or file_count < 1
        or file_count != len(normalized_files)
    ):
        errors.append("PAYLOAD_SNAPSHOT_INVALID:file_count")

    digest = snapshot.get("digest")
    if not _is_sha256_digest(digest):
        errors.append("PAYLOAD_SNAPSHOT_INVALID:digest")
    elif normalized_files:
        try:
            from governance_rule.execution.integrity.package_integrity import (
                snapshot_digest,
            )
        except ImportError:
            errors.append("PAYLOAD_SNAPSHOT_VALIDATOR_UNAVAILABLE")
        else:
            if snapshot_digest(normalized_files) != digest:
                errors.append("PAYLOAD_SNAPSHOT_INVALID:digest")

    return list(dict.fromkeys(errors))


def _collect_payload_files(root: Path, roots: Iterable[str]) -> dict[str, str]:
    from governance_rule.execution.integrity.package_integrity import (
        SOURCE_IGNORED_DIRECTORY_NAMES,
        collect_file_hashes,
    )

    normalized_roots = tuple(roots)
    excluded = frozenset({"manifest.json"}) if "." in normalized_roots else frozenset()
    return collect_file_hashes(
        root,
        normalized_roots,
        ignored_directory_names=SOURCE_IGNORED_DIRECTORY_NAMES,
        excluded_relative_paths=excluded,
    )


def build_release_payload_snapshot(
    release_root: str | os.PathLike[str],
    *,
    roots: Iterable[str] = REQUIRED_PAYLOAD_ROOTS,
) -> dict[str, Any]:
    """Build the manifest payload snapshot for an isolated release bundle."""
    from governance_rule.execution.integrity.package_integrity import (
        snapshot_digest,
    )

    normalized_roots = tuple(roots)
    files = _collect_payload_files(Path(os.fspath(release_root)), normalized_roots)
    return {
        "schema_version": PAYLOAD_SNAPSHOT_SCHEMA,
        "algorithm": "sha256",
        "roots": list(normalized_roots),
        "file_count": len(files),
        "files": files,
        "digest": snapshot_digest(files),
    }


def _payload_root_coverage_errors(root: Path, roots: tuple[str, ...]) -> list[str]:
    if "." in roots:
        return []
    try:
        actual_roots = {
            entry.name
            for entry in os.scandir(root)
            if entry.name != "manifest.json"
        }
    except OSError as error:
        return [f"PAYLOAD_ROOTS_UNREADABLE:{error.__class__.__name__}"]
    return [
        f"PAYLOAD_ROOT_UNCOVERED:{name}"
        for name in sorted(actual_roots - set(roots))
    ]


def _payload_mismatches(
    expected: dict[str, str], actual: dict[str, str]
) -> list[str]:
    mismatches = [
        *(f"missing:{path}" for path in sorted(set(expected) - set(actual))),
        *(f"unexpected:{path}" for path in sorted(set(actual) - set(expected))),
        *(
            f"changed:{path}"
            for path in sorted(set(expected) & set(actual))
            if expected[path] != actual[path]
        ),
    ]
    return mismatches[:_MAX_PAYLOAD_MISMATCHES]


def _verify_payload_snapshot(
    root: Path, snapshot: Any
) -> tuple[list[str], list[str], str]:
    errors = validate_payload_snapshot(snapshot)
    if errors:
        return errors, [], ""

    roots = tuple(snapshot["roots"])
    errors.extend(_payload_root_coverage_errors(root, roots))
    actual_files: dict[str, str] = {}
    actual_digest = ""
    try:
        actual_files = _collect_payload_files(root, roots)
        from governance_rule.execution.integrity.package_integrity import (
            snapshot_digest,
        )

        actual_digest = snapshot_digest(actual_files)
    except (ImportError, OSError, ValueError) as error:
        errors.append(
            f"PAYLOAD_SNAPSHOT_UNREADABLE:{error.__class__.__name__}"
        )
        return list(dict.fromkeys(errors)), [], actual_digest

    mismatches = _payload_mismatches(snapshot["files"], actual_files)
    if mismatches:
        errors.append("PAYLOAD_SNAPSHOT_MISMATCH")
    if actual_digest != snapshot["digest"]:
        errors.append("PAYLOAD_DIGEST_MISMATCH")
    return list(dict.fromkeys(errors)), mismatches, actual_digest


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
