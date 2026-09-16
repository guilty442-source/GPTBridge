"""Orchestrator for the governance runtime audit."""

from __future__ import annotations

import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from governance_rule.permission_directory.registries.permissions.source_ownership import (
    source_ownership_errors,
)

from .audit_artifacts import (
    check_authority_marker,
    check_codex_consistency,
    check_contract_handshake,
    check_data_lineage,
    check_ddl_audit,
    check_embedded_browser,
    check_git_tiers,
    check_metadata_contract,
    check_permission_snapshot,
    check_permission_snapshot_helper,
    check_provenance_helper,
    check_reconcile_modules,
    check_schema_ownership_lock,
    check_sql_migrations,
    check_sqlite_template,
    check_write_provenance,
)
from .audit_authority import (
    check_architecture_sources,
    check_identity_permissions,
    check_main_system_source,
    check_authority_policy,
    check_repair_policy,
    check_shared_layer_policy,
    check_shared_layer_structure,
    check_third_party_inventory,
    check_tool_isolation_hardening,
)
from .audit_activation import check_activation_states
from .audit_directories import check_directory_audit
from .audit_formal_rules import (
    check_formal_rules,
    check_implementation_obligations,
)
from .audit_manifests import (
    check_tool_identity_registration,
    check_tool_manifests,
)
from .audit_protected import (
    check_forbidden_legacy,
    check_protected_sources,
)
from .audit_runtime_contracts import check_runtime_contracts
from .audit_self_health import _verify_self_health_test_files

PROJECT_ROOT = Path(__file__).resolve().parents[3]

# ---------------------------------------------------------------------------
# Content-addressed PASS cache (audit speed)
#
# The full audit repeats the same read-only checks many times per session
# (commit gate, startup gate, periodic scheduler).  A recorded PASS is reused
# only while every audited input is byte- and attribute-identical: the
# fingerprint covers file size, mtime and Windows attributes (so clearing a
# protected read-only flag invalidates it) for every file under the audited
# roots, plus added/removed files.  Failures are never cached, and
# ``GPTBRIDGE_AUDIT_CACHE=0`` disables the cache entirely.
# ---------------------------------------------------------------------------
_AUDIT_CACHE_VERSION = 1
_AUDIT_CACHE_RELATIVE = (
    "main-system",
    "runtime",
    "state",
    "governance-audit-cache.json",
)
_FINGERPRINT_ROOTS = (
    "governance_rule",
    "main-system",
    "shared-layer",
    "Standalone tools",
)
_FINGERPRINT_EXCLUDED_DIRS = frozenset(
    {
        "node_modules",
        ".venv",
        "__pycache__",
        ".git",
        ".kilo",
        "runtime",
        "dist",
        "dist-ui",
        "data",
        "temp",
        "cache",
        "backups",
        "release",
        "logs",
        "browser-profile",
        "browser-profiles",
    }
)
# The audit itself appends/touches these files on every run; they are
# outputs, not audited inputs, so they must not invalidate the fingerprint.
_FINGERPRINT_EXCLUDED_FILES = frozenset(
    {
        "codex_entry_state.json",
        "codex_read_audit.jsonl",
        "git_tier_audit.jsonl",
    }
)


def _audit_cache_enabled() -> bool:
    value = str(os.environ.get("GPTBRIDGE_AUDIT_CACHE", "1")).strip().casefold()
    return value not in {"0", "false", "off", "no"}


def _audit_input_fingerprint(root: Path) -> str | None:
    """Fingerprint every audited file (path, size, mtime, attributes).

    Uses ``os.scandir()`` instead of ``os.walk()`` + ``path.stat()`` to
    avoid a second stat syscall per file — on Windows the directory entry
    already carries size/mtime/attributes.
    """
    digest = hashlib.sha256()
    excluded_dirs = {d.casefold() for d in _FINGERPRINT_EXCLUDED_DIRS}
    try:
        for relative in _FINGERPRINT_ROOTS:
            base = root / relative
            if not base.is_dir():
                digest.update(f"missing:{relative}\n".encode("utf-8"))
                continue
            _fingerprint_dir(base, root, digest, excluded_dirs)
    except OSError:
        return None
    return digest.hexdigest()


def _fingerprint_dir(
    dir_path: Path,
    root: Path,
    digest: "hashlib._Hash",
    excluded_dirs: set[str],
) -> None:
    """Recursively fingerprint a directory using os.scandir."""
    try:
        entries = sorted(os.scandir(dir_path), key=lambda e: e.name)
    except OSError:
        return
    for entry in entries:
        name = entry.name
        if entry.is_dir(follow_symlinks=False):
            if name.casefold() in excluded_dirs:
                continue
            _fingerprint_dir(Path(entry.path), root, digest, excluded_dirs)
        elif entry.is_file(follow_symlinks=False):
            casefolded = name.casefold()
            if name in _FINGERPRINT_EXCLUDED_FILES or casefolded.endswith(".tmp"):
                continue
            try:
                stat = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            attributes = int(getattr(stat, "st_file_attributes", 0) or 0)
            rel = Path(entry.path).relative_to(root).as_posix()
            digest.update(
                (
                    f"{rel}:{stat.st_size}:"
                    f"{stat.st_mtime_ns}:{attributes}\n"
                ).encode("utf-8", "surrogateescape")
            )


def _load_audit_cache(root: Path) -> dict[str, object] | None:
    try:
        payload = json.loads(root.joinpath(*_AUDIT_CACHE_RELATIVE).read_text("utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _store_audit_cache(root: Path, fingerprint: str, include_self_health: bool = True) -> None:
    path = root.joinpath(*_AUDIT_CACHE_RELATIVE)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(
                {
                    "version": _AUDIT_CACHE_VERSION,
                    "passed": True,
                    "fingerprint": fingerprint,
                    "include_self_health": include_self_health,
                    "checked_at": datetime.now(timezone.utc).isoformat(),
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    except OSError:
        pass


def _collect(check: Callable[[Path, list[str]], None], root: Path) -> list[str]:
    errors: list[str] = []
    check(root, errors)
    return errors


def _manifest_pair(root: Path) -> list[str]:
    """Manifest checks are dependent — identity registration needs the
    manifest tool ids — so they run as one task."""
    errors: list[str] = []
    manifest_tool_ids, _ = check_tool_manifests(root, errors)
    check_tool_identity_registration(root, errors, manifest_tool_ids)
    return errors


def audit_runtime_governance(
    project_root: Path = PROJECT_ROOT,
    *,
    include_self_health: bool = True,
    use_cache: bool = True,
) -> list[str]:
    """Run governance checks and return errors.

    Checks are independent (each opens its own file handles and codex
    connections) so they run concurrently; the merged error list preserves
    the original declaration order, keeping results deterministic.
    Startup may omit subprocess-based test collection; release and explicit
    audits retain the complete self-health barrier by default.

    When ``use_cache`` is set and the full barrier is requested, an identical
    audited-input fingerprint reuses a recorded PASS (see ``_audit_input_fingerprint``);
    only PASS results are ever cached.
    """
    root = project_root.resolve()

    fingerprint: str | None = None
    if use_cache and _audit_cache_enabled():
        fingerprint = _audit_input_fingerprint(root)
        cached = _load_audit_cache(root)
        if (
            fingerprint
            and cached is not None
            and cached.get("version") == _AUDIT_CACHE_VERSION
            and cached.get("passed") is True
            and cached.get("fingerprint") == fingerprint
            # Cache is valid only when the self-health mode matches: a
            # full-audit PASS covers the commit-gate subset, but a
            # commit-gate PASS does not cover the full audit.
            and (
                cached.get("include_self_health") is True
                or cached.get("include_self_health") == include_self_health
            )
        ):
            return []

    errors: list[str] = []

    checks: list[Callable[[Path], list[str]]] = [
        source_ownership_errors,
        lambda r: _collect(check_authority_policy, r),
        lambda r: _collect(check_architecture_sources, r),
        lambda r: _collect(check_third_party_inventory, r),
        lambda r: _collect(check_shared_layer_policy, r),
        lambda r: _collect(check_repair_policy, r),
        lambda r: _collect(check_protected_sources, r),
        lambda r: _collect(check_forbidden_legacy, r),
        lambda r: _collect(check_runtime_contracts, r),
        lambda r: _collect(check_identity_permissions, r),
        lambda r: _collect(check_main_system_source, r),
        lambda r: _collect(check_shared_layer_structure, r),
        lambda r: _collect(check_tool_isolation_hardening, r),
        _manifest_pair,
        lambda r: _collect(check_codex_consistency, r),
        lambda r: _collect(check_directory_audit, r),
        lambda r: _collect(check_activation_states, r),
        lambda r: _collect(check_formal_rules, r),
        lambda r: _collect(check_implementation_obligations, r),
        lambda r: _collect(check_git_tiers, r),
        lambda r: _collect(check_metadata_contract, r),
        lambda r: _collect(check_reconcile_modules, r),
        lambda r: _collect(check_sql_migrations, r),
        lambda r: _collect(check_sqlite_template, r),
        lambda r: _collect(check_data_lineage, r),
        lambda r: _collect(check_authority_marker, r),
        lambda r: _collect(check_write_provenance, r),
        lambda r: _collect(check_provenance_helper, r),
        lambda r: _collect(check_permission_snapshot, r),
        lambda r: _collect(check_schema_ownership_lock, r),
        lambda r: _collect(check_ddl_audit, r),
        lambda r: _collect(check_contract_handshake, r),
        lambda r: _collect(check_permission_snapshot_helper, r),
        lambda r: _collect(check_embedded_browser, r),
    ]
    if include_self_health:
        checks.append(
            lambda r: _collect(_verify_self_health_test_files, r)
        )

    # Executor.map preserves submission order; a check raising is contained
    # as an error entry rather than aborting the remaining checks.
    with ThreadPoolExecutor(
        max_workers=min(8, len(checks)),
        thread_name_prefix="governance-audit",
    ) as executor:
        results = list(executor.map(lambda check: check(root), checks))
    for result in results:
        errors.extend(result)

    if not errors and fingerprint:
        _store_audit_cache(root, fingerprint, include_self_health)
    return errors
