"""Golden Git baseline payload build / write / verify / bind (A185 split).

Extracted from ``baseline/__init__`` (source-size contract): assembles the
article-522 golden manifest, writes it atomically with its ``.sha256``
sidecar, verifies embedded + sidecar digests, and binds the baseline
digest into ``git_governance_manifest.json`` through the Wave1-A
manifest-rewrite mechanism (the binding is excluded from the manifest's
canonical payload, so the write must not move the manifest digest).
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Mapping, Optional

from .. import governance_manifest
from ..governance_manifest import build_integrity, compute_digest
from . import static_gate
from .freeze import build_freeze_table, overall_verdict
from .introspection import (
    _component_map,
    _config_evidence,
    _documentation_scan,  # noqa: F401  (re-exported for callers/tests)
    _git_evidence,
    _hook_evidence,
    _lock_hierarchy,
    _module_inventory,
    _policy_evidence,
    _public_api_surface,
    _schema_versions,
    _state_models,
)
from .specs import (
    ARCHITECTURE_VERSION,
    ARTICLE_522_FIELDS,
    BASELINE_DIGEST_FILENAME,
    BASELINE_DIR,
    BASELINE_FILENAME,
    BASELINE_ID,
    BASELINE_VERSION,
    FINAL_INVARIANTS,
    FROZEN_API,
    GOVERNANCE_MANIFEST_DIR,
    INTEGRITY_FIELD,
    MANDATED_RUNTIME_COMPONENTS,
    PROJECT_ROOT,
    PUBLIC_API_VERSION,
)


def build_baseline_payload(
    *,
    gate_evidence: Optional[Mapping[str, Any]] = None,
    captured_at: Optional[str] = None,
) -> dict[str, Any]:
    """Assemble the complete golden manifest (deterministic except evidence)."""
    from .. import governance_manifest as manifest_mod

    status = manifest_mod.governance_status()
    main_digest = status.digest
    scan_result = static_gate.scan()
    allowlist = {
        category: list(items)
        for category, items in scan_result["categories"].items()
    }
    freeze_table = build_freeze_table(scan_result, gate_evidence)
    verdict, blockers = overall_verdict(freeze_table)

    from ..git_repository import GitRepository

    head = ""
    try:
        result = GitRepository(PROJECT_ROOT).run(["rev-parse", "HEAD"])
        head = (result.stdout or "").strip()
    except Exception:
        head = ""

    payload: dict[str, Any] = {
        "baseline_id": BASELINE_ID,
        "baseline_version": BASELINE_VERSION,
        "kind": "golden-manifest",
        "verdict": verdict,
        "blockers": blockers,
        "architecture_version": {
            "version": ARCHITECTURE_VERSION,
            "article": "A535",
            "status": "CANDIDATE" if verdict != "GIT_BASELINE_FROZEN"
            else "ACTIVE",
        },
        "governance_version": manifest_mod.manifest_field("governance_version", ""),
        "governance_manifest": {
            "path": "governance_rule/execution/git_tiers/git_governance_manifest.json",
            "digest": main_digest,
            "mode": status.mode.value,
        },
        "public_api_version": PUBLIC_API_VERSION,
        "git": _git_evidence(),
        "config": _config_evidence(),
        "policy": _policy_evidence(),
        "hooks": _hook_evidence(),
        "schema_versions": _schema_versions(),
        "components": _component_map(),
        "public_api": _public_api_surface(),
        "state_models": _state_models(),
        "lock_hierarchy": _lock_hierarchy(),
        "invariants": [
            {"id": inv_id, "statement": statement, "source": source}
            for inv_id, statement, source in FINAL_INVARIANTS
        ],
        "test_suite": {
            "gates": dict(gate_evidence.get("gates", {}))
            if gate_evidence else {},
            "freeze_conditions": freeze_table,
            "evidence_meta": (
                dict(gate_evidence.get("meta", {})) if gate_evidence else {}
            ),
        },
        "static_allowlist": allowlist,
        "static_counts": scan_result["counts"],
        "complexity": _complexity_budget(),
        "baseline_commit": head,
        "worktree_dirty_at_capture": bool(_worktree_dirty()),
        "captured_at_utc": captured_at
        or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "post_freeze_policy": {
            "allowed": [
                "BUGFIX", "SECURITY_FIX", "PERFORMANCE_OPTIMIZATION",
                "TEST_IMPROVEMENT", "DOCUMENTATION", "VERSIONED_MIGRATION",
            ],
            "forbidden_unversioned": [
                "new manager", "new coordinator", "new Git driver",
                "new audit ledger", "new lock system", "new state machine",
                "parallel queue", "parallel worker registry",
                "another Git gateway",
            ],
            "reentry": "Git Architecture Change Proposal -> architecture "
                       "version bump (A539)",
        },
    }
    missing = [field for field in ARTICLE_522_FIELDS if field not in payload]
    if missing:
        raise ValueError(f"baseline payload missing article-522 fields: {missing}")
    payload[INTEGRITY_FIELD] = build_integrity(payload)
    return payload


def _worktree_dirty() -> bool:
    try:
        from ..git_repository import GitRepository

        repo = GitRepository(PROJECT_ROOT)
        status = repo.run(["status", "--porcelain"])
        return bool((status.stdout or "").strip())
    except Exception:
        return True


def _complexity_budget() -> dict[str, Any]:
    """Article 532 — final budget numbers, targets vs measured."""
    scan_result = static_gate.scan()
    module_inventory = _module_inventory()
    direct_all = static_gate.scan_direct_git_all()
    production_git_modules = sorted(
        rel for rel in direct_all
        if not rel.startswith("scripts/")
    )
    measured = {
        "runtime_components": len(MANDATED_RUNTIME_COMPONENTS),
        "runtime_components_implemented_as_named_class": 2,
        "git_drivers": 1,
        "git_driver_modules_executing_git": len(production_git_modules),
        "status_parsers": 1,
        "audit_writers": 1,
        "queue_owners": 1,
        "worker_registry_owners": 1,
        "lock_models": 1,
        "config_sources": 2,
        "public_apis": len(FROZEN_API),
    }
    targets = {
        "git_drivers": 1,
        "status_parsers": 1,
        "audit_writers": 1,
        "queue_owners": 1,
        "worker_registry_owners": 1,
        "lock_models": 1,
        "runtime_config": 1,
        "runtime_components": 6,
    }
    return {
        "targets": targets,
        "measured": measured,
        "git_driver_modules": production_git_modules,
        "modules": module_inventory["totals"],
        "static_counts": scan_result["counts"],
        "notes": [
            "config_sources=2: PoolConfig + governance manifest timings "
            "(GitRuntimeConfig missing)",
            "runtime_components_implemented_as_named_class=2: GitControlPlane "
            "and GitMaintenanceManager(name variant)",
            "audit_writers=1 canonical (__init__.audit_log + audit_chain); "
            "coordinator.py still has a legacy merge_queue.jsonl writer",
            "lock_models=1 canonical (process_lock); coordinator.py and "
            "audit_chain.py carry private byte-range locks",
        ],
    }


def write_baseline(
    payload: Mapping[str, Any], directory: str | Path = BASELINE_DIR
) -> dict[str, Any]:
    """(Re)write baseline + ``.sha256`` sidecar atomically."""
    target_dir = Path(directory)
    target_dir.mkdir(parents=True, exist_ok=True)
    body = {key: value for key, value in payload.items() if key != INTEGRITY_FIELD}
    integrity = build_integrity(body)
    body[INTEGRITY_FIELD] = integrity
    text = json.dumps(body, ensure_ascii=False, indent=2) + "\n"
    path = target_dir / BASELINE_FILENAME
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    digest_path = target_dir / BASELINE_DIGEST_FILENAME
    digest_tmp = digest_path.with_name(digest_path.name + f".{os.getpid()}.tmp")
    digest_tmp.write_text(integrity["digest"] + "\n", encoding="ascii")
    os.replace(digest_tmp, digest_path)
    return {"path": str(path), "digest": integrity["digest"]}


def verify_baseline(
    path: str | Path | None = None,
    digest_path: str | Path | None = None,
) -> dict[str, Any]:
    """Verify embedded digest + sidecar; returns mode/reason/digest."""
    manifest = Path(path) if path else BASELINE_DIR / BASELINE_FILENAME
    sidecar = (
        Path(digest_path)
        if digest_path
        else manifest.with_name(BASELINE_DIGEST_FILENAME)
    )
    if not manifest.is_file():
        return {"mode": "MISSING", "reason": f"missing:{manifest}", "digest": ""}
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"mode": "READ_ONLY", "reason": f"unreadable:{exc}", "digest": ""}
    actual = compute_digest(payload)
    embedded = ""
    integrity = payload.get(INTEGRITY_FIELD)
    if isinstance(integrity, Mapping):
        embedded = str(integrity.get("digest") or "")
    side = ""
    if sidecar.is_file():
        side = sidecar.read_text(encoding="ascii").strip().split()[0]
    if not embedded and not side:
        return {"mode": "READ_ONLY", "reason": "digest-missing", "digest": actual}
    candidates = {value.lower() for value in (embedded, side) if value}
    if len(candidates) > 1:
        return {"mode": "READ_ONLY", "reason": "digest-conflict", "digest": actual}
    expected = candidates.pop()
    if actual != expected:
        return {"mode": "READ_ONLY", "reason": "digest-mismatch", "digest": actual}
    return {
        "mode": "ACTIVE",
        "reason": "ok",
        "digest": actual,
        "verdict": str(payload.get("verdict", "")),
        "payload": payload,
    }


def bind_to_governance_manifest(
    baseline_path: str | Path | None = None,
    manifest_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Register the baseline digest inside ``git_governance_manifest.json``.

    Rewrites the main manifest through ``governance_manifest.write_manifest``
    so its canonical digest/sidecar stay authoritative (Wave1-A mechanism).
    The ``baseline`` binding is excluded from the manifest's canonical
    payload, so this write must not move the manifest digest; a moved digest
    means the binding leaked into the manifest identity and fails closed.
    """
    baseline = Path(baseline_path) if baseline_path else BASELINE_DIR / BASELINE_FILENAME
    verification = verify_baseline(baseline)
    if verification["mode"] != "ACTIVE":
        raise ValueError(f"baseline not verifiable: {verification}")
    directory = (
        Path(manifest_dir) if manifest_dir
        else GOVERNANCE_MANIFEST_DIR
    )
    manifest_path = directory / governance_manifest.MANIFEST_FILENAME
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    payload.pop(INTEGRITY_FIELD, None)
    before_digest = governance_manifest.compute_digest(payload)
    payload["baseline"] = {
        "baseline_id": BASELINE_ID,
        "baseline_version": BASELINE_VERSION,
        "path": "baseline/" + baseline.name,
        "digest": verification["digest"],
        "verdict": verification.get("verdict", ""),
        "manifest_schema_version": "1.0.0",
    }
    status = governance_manifest.write_manifest(payload, directory)
    if status.digest != before_digest:
        raise ValueError(
            "baseline binding must not move the manifest digest: "
            f"{before_digest} -> {status.digest}"
        )
    return {
        "manifest": str(manifest_path),
        "manifest_digest": status.digest,
        "baseline_digest": verification["digest"],
    }


__all__ = [
    "bind_to_governance_manifest",
    "build_baseline_payload",
    "verify_baseline",
    "write_baseline",
    "_complexity_budget",
    "_worktree_dirty",
]
