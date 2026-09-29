"""Golden Git baseline introspection helpers (A185 split).

Extracted from ``baseline/__init__`` (source-size contract): module
inventory, frozen-API surface resolution, state models, lock hierarchy,
git/config/policy/hook evidence, schema versions, the component map and
the documentation staleness scan.  These feed ``payload.build_baseline_payload``.
"""
from __future__ import annotations

import ast
import hashlib
import importlib
import inspect
import json
import shutil
from typing import Any

from .. import governance_manifest
from .specs import (
    EXPECTED_LOCKS,
    EXPECTED_STATES,
    FROZEN_API,
    GIT_TIERS_DIR,
    MANDATED_RUNTIME_COMPONENTS,
    PROJECT_ROOT,
    PUBLIC_API_VERSION,
)


def _module_inventory() -> dict[str, Any]:
    modules: list[dict[str, Any]] = []
    total_functions = total_classes = total_lines = 0
    for path in sorted(GIT_TIERS_DIR.glob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        functions = sum(
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            for node in ast.walk(tree)
        )
        classes = sum(isinstance(node, ast.ClassDef) for node in ast.walk(tree))
        lines = len(path.read_text(encoding="utf-8", errors="replace").splitlines())
        total_functions += functions
        total_classes += classes
        total_lines += lines
        modules.append({
            "module": path.name,
            "functions": functions,
            "classes": classes,
            "lines": lines,
        })
    return {
        "modules": modules,
        "totals": {
            "modules": len(modules),
            "functions": total_functions,
            "classes": total_classes,
            "lines": total_lines,
        },
    }


def _resolve_module(module_name: str):
    """Import a ``git_tiers`` submodule by its short name."""
    return importlib.import_module(
        f"governance_rule.execution.git_tiers.{module_name}"
    )


def _signature_of(dotted: str) -> dict[str, Any]:
    module_name, _, attr_path = dotted.partition(":")
    module = _resolve_module(module_name)
    target: Any = module
    for part in attr_path.split("."):
        target = getattr(target, part)
    if inspect.isclass(target):
        methods: dict[str, str] = {}
        for name in FROZEN_API.get(dotted, ()):  # methods live in the spec
            member = getattr(target, name, None)
            methods[name] = (
                str(inspect.signature(member)) if callable(member) else "MISSING"
            )
        return {"kind": "class", "methods": methods}
    try:
        signature = str(inspect.signature(target))
    except (TypeError, ValueError):
        signature = "(...)"
    return {"kind": "callable", "signature": signature}


def _public_api_surface() -> dict[str, Any]:
    surface: dict[str, Any] = {}
    for dotted, methods in FROZEN_API.items():
        if ":" not in dotted:
            try:
                module_name, _, obj_name = dotted.rpartition(".")
                module = _resolve_module(module_name)
                target = getattr(module, obj_name)
            except (ImportError, AttributeError) as exc:
                surface[dotted] = {"status": "MISSING", "error": str(exc)}
                continue
            if inspect.isclass(target):
                entries: dict[str, str] = {}
                for name in methods:
                    member = getattr(target, name, None)
                    entries[name] = (
                        str(inspect.signature(member))
                        if callable(member) else "MISSING"
                    )
                surface[dotted] = {"kind": "class", "methods": entries}
            else:
                try:
                    surface[dotted] = {
                        "kind": "callable",
                        "signature": str(inspect.signature(target)),
                    }
                except (TypeError, ValueError):
                    surface[dotted] = {"kind": "callable", "signature": "(...)"}
        else:
            try:
                surface[dotted] = _signature_of(dotted)
            except (ImportError, AttributeError) as exc:
                surface[dotted] = {"status": "MISSING", "error": str(exc)}
    canonical = json.dumps(surface, sort_keys=True, ensure_ascii=False)
    return {
        "version": PUBLIC_API_VERSION,
        "surface": surface,
        "surface_digest": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    }


def _state_models() -> dict[str, Any]:
    from .. import git_control_plane, worker_pool_types

    actual: dict[str, list[str]] = {
        "WorkerState": [member.value for member in worker_pool_types.WorkerState],
        "GlobalGitState": [
            member.value for member in git_control_plane.GitGlobalState
        ],
        "HealthStatus": [member.value for member in git_control_plane.HealthStatus],
        "PoolHealth": [member.value for member in worker_pool_types.PoolHealth],
        "AllocationResult": [
            member.value for member in worker_pool_types.AllocationResult
        ],
    }
    from .. import merge_queue

    actual["QueueState"] = sorted(merge_queue.QUEUE_STATUSES)
    models: dict[str, Any] = {}
    for name, expected in EXPECTED_STATES.items():
        current = actual.get(name)
        if current is None:
            models[name] = {
                "status": "NOT_IMPLEMENTED",
                "expected": list(expected),
                "actual": [],
                "missing": list(expected),
                "extra": [],
            }
            continue
        missing = sorted(set(expected) - set(current))
        extra = sorted(set(current) - set(expected))
        models[name] = {
            "status": "FROZEN_MATCH" if not missing and not extra else "DIVERGENT",
            "expected": list(expected),
            "actual": current,
            "missing": missing,
            "extra": extra,
        }
    models["_other_enums"] = {
        name: value for name, value in actual.items() if name not in EXPECTED_STATES
    }
    return models


def _lock_hierarchy() -> dict[str, Any]:
    from .. import locks

    order = list(locks.LOCK_ORDER)
    return {
        "model": "locks.LOCK_ORDER + process_lock.ProcessFileLock",
        "actual_order": order,
        "expected_order": list(EXPECTED_LOCKS),
        "canonical_names_match": False,
        "duplicate_implementations": [
            "coordinator.py:70-103 merge-queue byte-range lock",
            "audit_chain.py:56-81 audit append byte-range lock",
        ],
        "note": (
            "canonical ordered names are supervisor-registry/workspace-sync/"
            "merge-queue/worktree/audit-append; the article 508 names "
            "(SUPERVISOR, WORKER_REGISTRY, ...) are not registered"
        ),
    }


def _git_evidence() -> dict[str, Any]:
    from .. import compatibility

    version = compatibility.detected_git_version()
    executable = shutil.which("git") or ""
    return {
        "executable": executable,
        "version": version,
        "minimum": governance_manifest.manifest_field("minimum_git_version"),
        "maximum_tested": governance_manifest.manifest_field(
            "maximum_tested_git_version"
        ),
    }


def _config_evidence() -> dict[str, Any]:
    from .. import worker_pool_types
    from ..governance_manifest import timing

    manifest = governance_manifest.governance_status()
    timings = dict(manifest.payload.get("timings", {})) if manifest.ok else {}
    pool_defaults = {
        key: getattr(worker_pool_types.PoolConfig(), key)
        for key in worker_pool_types.PoolConfig.__dataclass_fields__
    }
    payload = {
        "manifest_timings": timings,
        "pool_config_defaults": {
            key: value for key, value in pool_defaults.items()
            if isinstance(value, (int, float, str, bool, tuple))
        },
    }
    canonical = json.dumps(payload, sort_keys=True, default=str)
    return {
        "model": (
            "PoolConfig (worker_pool_types) + governance manifest timings; "
            "GitRuntimeConfig (article 521) is NOT IMPLEMENTED"
        ),
        "digest": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "sources": [
            "git_governance_manifest.json:timings",
            "worker_pool_types.PoolConfig defaults",
        ],
    }


def _policy_evidence() -> dict[str, Any]:
    from .. import branch_policy, capability, git_control_plane

    plane = git_control_plane.GitControlPlane(PROJECT_ROOT)
    digests = {
        "tier_policy": plane.policy_digest(),
        "branch_policy": branch_policy.policy_digest(),
        "capability_policy": capability.policy_digest(),
    }
    return {
        "version": plane.policy_version(),
        "digests": digests,
    }


def _hook_evidence() -> dict[str, Any]:
    from .. import hook_versioning

    templates = hook_versioning.verify_governed_templates()
    hooks = {
        name: {
            "version": report["version"],
            "expected_version": report["expected_version"],
            "digest": report["digest"],
            "ok": report["ok"],
        }
        for name, report in templates["hooks"].items()
    }
    return {
        "template_dir": templates["template_dir"],
        "hooks": hooks,
        "templates_ok": templates["ok"],
        "live_generation": hook_versioning.current_hook_generation(
            root=PROJECT_ROOT
        ),
    }


def _schema_versions() -> dict[str, Any]:
    status = governance_manifest.governance_status()
    payload = status.payload or {}
    return {
        field: payload.get(field, "")
        for field in governance_manifest.REQUIRED_FIELDS
        if field.endswith("_version") or field == "hook_version"
    }


def _component_map() -> dict[str, Any]:
    runtime = {
        "GitControlPlane": {
            "class": "git_control_plane.GitControlPlane",
            "status": "IMPLEMENTED",
        },
        "WorkerManager": {
            "class": "worker_pool.WorkerPool + worker_registry.PoolRegistry "
                     "+ worker_lifecycle/worker_reconcile",
            "status": "RESPONSIBILITY_PRESENT (no class named WorkerManager)",
        },
        "IntegrationManager": {
            "class": "coordinator.Coordinator + merge_queue.MergeQueue "
                     "+ merge_precheck + workspace_sync",
            "status": "RESPONSIBILITY_PARTIAL (no class named IntegrationManager)",
        },
        "GitExecutionGateway": {
            "class": "git_repository.GitRepository.run + capability_gate adapter",
            "status": "NOT_IMPLEMENTED as gateway (driver doubles as entrypoint)",
        },
        "EvidenceManager": {
            "class": "audit_chain + audit_records + snapshot + recovery",
            "status": "RESPONSIBILITY_PARTIAL (no class named EvidenceManager)",
        },
        "MaintenanceManager": {
            "class": "git_maintenance.GitMaintenanceManager",
            "status": "IMPLEMENTED (name variant)",
        },
    }
    driver = {
        "GitProcessDriver": {
            "class": "git_repository.GitRepository.run",
            "status": "RESPONSIBILITY_PRESENT (single subprocess driver)",
        }
    }
    support = {
        "GitCommandNormalizer": {
            "class": "command_normalizer.normalize_command",
            "status": "IMPLEMENTED",
        },
        "RepositoryObserver": {
            "class": "porcelain.status_v2 + git_control_plane collectors",
            "status": "RESPONSIBILITY_PRESENT",
        },
        "BranchPolicy": {
            "class": "branch_policy",
            "status": "IMPLEMENTED",
        },
        "GitRuntimeConfig": {
            "class": "",
            "status": "NOT_IMPLEMENTED",
        },
        "LockManager": {
            "class": "locks.LOCK_ORDER + process_lock.ProcessFileLock",
            "status": "PARTIAL (names diverge from article 508)",
        },
        "CapabilityVerifier": {
            "class": "capability_verify.verify_capability",
            "status": "IMPLEMENTED",
        },
        "GitInvariants": {
            "class": "fault_injection.assert_git_invariants (test-only)",
            "status": "PARTIAL (no production module)",
        },
    }
    return {
        "runtime": runtime,
        "driver": driver,
        "support": support,
        "mandated_runtime": list(MANDATED_RUNTIME_COMPONENTS),
        "modules": _module_inventory(),
    }


def _documentation_scan() -> dict[str, Any]:
    """Article 534 — docs must not keep stale legacy descriptions."""
    stale_markers = (
        "confirmed=True",
        "Coordinator direct Git",
        "main watcher",
        "LocalGitRepository",
    )
    docs = [
        PROJECT_ROOT / "docs" / "worktree-architecture.md",
        PROJECT_ROOT / "docs" / "module-topology.md",
        PROJECT_ROOT / "AGENTS.md",
    ]
    findings: list[str] = []
    checked: list[str] = []
    for path in docs:
        if not path.is_file():
            continue
        checked.append(str(path.relative_to(PROJECT_ROOT)))
        text = path.read_text(encoding="utf-8", errors="replace")
        for marker in stale_markers:
            if marker in text:
                findings.append(f"{path.name}:{marker}")
    return {
        "checked": checked,
        "stale_findings": findings,
        "verdict": "PASS" if not findings else "FAIL",
    }


__all__ = [
    "_component_map", "_config_evidence", "_documentation_scan",
    "_git_evidence", "_hook_evidence", "_lock_hierarchy",
    "_module_inventory", "_policy_evidence", "_public_api_surface",
    "_resolve_module", "_schema_versions", "_signature_of",
    "_state_models",
]
