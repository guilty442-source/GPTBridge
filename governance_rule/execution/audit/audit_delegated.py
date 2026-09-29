"""Delegated manifest-check lane (G96) — split from audit_checks."""
from __future__ import annotations

import time
from concurrent.futures import (
    ThreadPoolExecutor,
    TimeoutError as FuturesTimeoutError,
    as_completed,
)
from pathlib import Path
from typing import Callable, Final

from governance_rule.permission_directory.registries.permissions.source_ownership import (
    source_ownership_errors,
)

from . import audit_checks as _ac
from .audit_checks import (
    AUDIT_FLOW_BUDGET_SECONDS,
    _audit_workers,
    _collect,
    _manifest_pair,
)


def _manifest_pair_into(root: Path, errors: list[str]) -> None:
    errors.extend(_manifest_pair(root))


def _source_ownership_into(root: Path, errors: list[str]) -> None:
    errors.extend(source_ownership_errors(root))


# Manifest ``delegated`` ids that do not name a top-level ``check_*``
# callable resolve onto the callable that owns their semantics (G96):
# semantic remainders of partially native-covered checks, aggregate
# members folded into their aggregate so nothing executes twice, and the
# manifest pair whose second member needs the first member's output.
_DELEGATED_CHECK_ALIASES: Final = {
    "protected-source-semantic": _ac.check_protected_sources,
    "codex-consistency-semantic": _ac.check_codex_consistency,
    "git-tiers-classify-failclosed": _ac.check_git_tiers,
    "embedded-browser-async-playwright": _ac.check_embedded_browser,
    "check_directory_schemas": _ac.check_directory_audit,
    "check_directory_catalog_coverage": _ac.check_directory_audit,
    "check_directory_identity_and_format": _ac.check_directory_audit,
    "check_provision_classification": _ac.check_directory_audit,
    "check_directory_relationships": _ac.check_directory_audit,
    "check_directory_mirror_parity": _ac.check_directory_audit,
    "check_directory_seal": _ac.check_directory_audit,
    "check_tool_manifests": _manifest_pair_into,
    "check_tool_identity_registration": _manifest_pair_into,
}


def _collect_guarded(
    check: Callable[[Path, list[str]], None], root: Path
) -> list[str]:
    """A raising check must surface as an error entry — never abort the
    remaining delegated checks (zero silent skip)."""
    try:
        return _collect(check, root)
    except Exception as error:  # noqa: BLE001 — fail-visible
        name = getattr(check, "__name__", repr(check))
        return [f"{name} raised: {error}"]


def run_delegated_checks(
    project_root: Path,
    manifest_checks: list[dict],
    *,
    budget_seconds: float = AUDIT_FLOW_BUDGET_SECONDS,
) -> tuple[list[str], list[str]]:
    """Execute the manifest's delegated Python checks (G96 closure).

    Every ``python-check:*`` delegated id must resolve to a callable —
    unresolvable ids are fail-closed errors.  Oracle checks that are
    neither native-covered nor delegated are added to the run (union
    parity), so a stale manifest cannot silently drop coverage.  Returns
    ``(errors, executed_check_ids)``.
    """
    root = project_root.resolve()
    errors: list[str] = []
    resolved: dict[Callable[[Path, list[str]], None], list[str]] = {}

    def _bind(fn: Callable[[Path, list[str]], None], cid: str) -> None:
        resolved.setdefault(fn, []).append(cid)

    delegated_ids: list[str] = []
    # Manifest-declared ``python`` targets are the authoritative
    # delegated→callable map (G96 single source); the id-name parse and
    # alias table below only cover rows that predate the field.
    record_targets: dict[str, str] = {}
    for record in manifest_checks:
        if record.get("kind") != "delegated":
            continue
        cid = str(record.get("id", ""))
        delegated_ids.append(cid)
        target = str(record.get("python", ""))
        if target:
            record_targets[cid] = target

    # Manifest-generation parity (G96): recompute the manifest from live
    # code — the file can lag check-module changes when mtimes lie (the
    # codex db trigger is a hint, not proof).  Content parity across every
    # check id AND payload is the formal compatibility check; drift is
    # fail-visible and the delegated union still runs so coverage never
    # silently narrows.
    from .export_audit_manifest import build_manifest

    fresh_checks = build_manifest(root).get("checks", [])
    fresh_map = {str(c.get("id", "")): c for c in fresh_checks}
    committed_map = {
        str(c.get("id", "")): c for c in manifest_checks
    }
    id_drift = sorted(set(committed_map) ^ set(fresh_map))
    payload_drift = sorted(
        cid
        for cid in set(committed_map) & set(fresh_map)
        if committed_map[cid] != fresh_map[cid]
    )
    if id_drift or payload_drift:
        errors.append(
            "manifest drift (regenerate manifest): "
            f"ids={id_drift[:8]} changed={payload_drift[:8]}"
        )
    fresh_ids = {
        cid for cid, c in fresh_map.items() if c.get("kind") == "delegated"
    }

    for cid in sorted(set(delegated_ids) | fresh_ids):
        name = (
            record_targets.get(cid)
            or str(fresh_map.get(cid, {}).get("python", ""))
            or (cid.split(":", 1)[1] if cid.startswith("python-check:") else "")
        )
        fn = _DELEGATED_CHECK_ALIASES.get(name)
        if fn is None and name.startswith("check_"):
            candidate = getattr(_ac, name, None)
            if callable(candidate):
                fn = candidate
        if fn is None:
            errors.append(
                f"delegated check id has no python implementation: {cid}"
            )
            continue
        _bind(fn, cid)

    # Oracle-only check: ``source_ownership_errors`` is not ``check_*``
    # named so the exporter never emits a delegated row for it.
    _bind(_source_ownership_into, "oracle-only:source_ownership_errors")

    started = time.monotonic()
    checks = [
        (cids, lambda r, f=fn: _collect_guarded(f, r))
        for fn, cids in resolved.items()
    ]
    # Hard deadline (G96): a hung delegated check cannot stall the request
    # past the audit budget — unfinished futures fail closed as timeouts.
    executor = ThreadPoolExecutor(
        max_workers=_audit_workers(len(checks)),
        thread_name_prefix="governance-audit-delegated",
    )
    try:
        pending = {
            executor.submit(check, project_root): cids
            for cids, check in checks
        }
        deadline = started + max(0.0, budget_seconds)
        unfinished: set = set(pending)
        try:
            for future in as_completed(
                pending,
                timeout=max(0.0, deadline - time.monotonic()),
            ):
                unfinished.discard(future)
                errors.extend(future.result())
        except FuturesTimeoutError:
            pass
        for future in unfinished:
            future.cancel()
            for cid in pending[future]:
                errors.append(
                    f"{cid}: delegated check timed out (fail-closed)"
                )
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    elapsed = time.monotonic() - started
    if elapsed > budget_seconds:
        errors.append(
            f"delegated audit budget exceeded: {elapsed:.3f}s > "
            f"{budget_seconds:.0f}s"
        )
    # Evidence fidelity (G96): every delegated id covered by a shared
    # callable counts as executed — the merged verdict must prove the
    # whole delegated set ran, not just the distinct callables.
    executed = [cid for cids in resolved.values() for cid in cids]
    return errors, executed
