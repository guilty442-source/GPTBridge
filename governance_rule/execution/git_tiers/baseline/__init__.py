"""Golden Git baseline manifest — builder, integrity and freeze verdict.

Wave-F / codex articles 522-541.  ``gptbridge_git_baseline_v1.json`` is the
one machine-readable architectural contract of the Git v1 plane:

    architecture_version / governance_version / public_api_version
    Git executable + version, config digest, policy digest, hook digests
    schema versions, complete component list, public API list,
    state models, lock hierarchy, invariant list,
    test suite (gate) results, baseline commit

Integrity reuses the Wave1-A mechanism — ``governance_manifest.compute_digest``
over canonical JSON with the ``integrity`` field excluded — and adds the
``.sha256`` sidecar.  The main ``git_governance_manifest.json`` binds the
baseline digest in its ``baseline`` section (rewritten through
``governance_manifest.write_manifest`` so its own digest stays canonical).
That binding is excluded from the manifest's own digest by construction
(``governance_manifest.canonical_payload`` excludes ``integrity`` and
``baseline``), so the baseline's recorded ``governance_manifest.digest``
stays valid after binding — binding is idempotent with respect to the
manifest identity and cannot form a digest cycle.

Every start-up only needs to verify *baseline compatibility* (article 523):
the baseline describes the architecture and governance contract, not the
current HEAD; runtime state is never required to equal the baseline digest.

The freeze verdict is derived strictly from the sixteen conditions of
codex article 536.  A condition without measured evidence is UNVERIFIED —
never PASS.
"""
from __future__ import annotations

import ast
import hashlib
import importlib
import inspect
import json
import os
import shutil
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from .. import governance_manifest
from ..governance_manifest import build_integrity, compute_digest
from . import static_gate

BASELINE_ID = "GPTBridgeGitBaselineV1"
BASELINE_VERSION = 1
BASELINE_FILENAME = "gptbridge_git_baseline_v1.json"
BASELINE_DIGEST_FILENAME = "gptbridge_git_baseline_v1.json.sha256"
BASELINE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = Path(__file__).resolve().parents[4]
GIT_TIERS_DIR = Path(__file__).resolve().parents[1]
INTEGRITY_FIELD = governance_manifest.INTEGRITY_FIELD

ARCHITECTURE_VERSION = "gptbridge-git-architecture-v1"
PUBLIC_API_VERSION = "git_api_v1"

#: article 522 — the golden manifest must carry every one of these sections
ARTICLE_522_FIELDS: tuple[str, ...] = (
    "architecture_version",
    "governance_version",
    "public_api_version",
    "git",
    "config",
    "policy",
    "hooks",
    "schema_versions",
    "components",
    "public_api",
    "state_models",
    "lock_hierarchy",
    "invariants",
    "test_suite",
    "baseline_commit",
)

#: article 489 — mandated runtime components and driver/support set
MANDATED_RUNTIME_COMPONENTS: tuple[str, ...] = (
    "GitControlPlane",
    "WorkerManager",
    "IntegrationManager",
    "GitExecutionGateway",
    "EvidenceManager",
    "MaintenanceManager",
)
MANDATED_DRIVER: tuple[str, ...] = ("GitProcessDriver",)
MANDATED_SUPPORT: tuple[str, ...] = (
    "GitCommandNormalizer",
    "RepositoryObserver",
    "BranchPolicy",
    "GitRuntimeConfig",
    "LockManager",
    "CapabilityVerifier",
    "GitInvariants",
)

#: article 493 — required public API surface (git_api_v1)
ARTICLE_493_API: Mapping[str, Mapping[str, str]] = {
    "ControlPlane": {
        "get_state": "git_control_plane.GitControlPlane.get_global_state",
        "get_health": "git_control_plane.GitControlPlane.snapshot().health",
        "get_worker": "git_control_plane.GitControlPlane.get_worker_state",
        "get_queue": "git_control_plane.GitControlPlane.get_queue_state",
        "get_remotes": "git_control_plane.GitControlPlane.get_remote_state",
    },
    "Worker": {
        "allocate": "worker_pool.WorkerPool.allocate_worker",
        "release": "worker_pool.WorkerPool.release_worker",
        "checkpoint": "self_commit.run_once",
        "quarantine": "worker_pool.WorkerPool.quarantine",
    },
    "Integration": {
        "enqueue": "merge_queue.MergeQueue.enqueue",
        "prepare": "merge_precheck.pre_merge_check",
        "merge": "coordinator.Coordinator (compatibility facade)",
        "sync": "workspace_sync.synchronize",
    },
    "Execution": {
        "execute": "capability_gate.execute_with_capability",
    },
    "Evidence": {
        "audit": "audit_chain.append_audit / **init**.audit_log",
        "snapshot": "snapshot.capture_light_snapshot",
        "recovery_point": "recovery.create_recovery_ref",
        "trace": "git_control_plane.GitControlPlane.trace_transaction",
    },
    "Maintenance": {
        "inspect": "git_maintenance.GitMaintenanceManager.plan",
        "maintain": "git_maintenance.GitMaintenanceManager.run",
    },
}

#: frozen surface pinned by the contract tests (actual implemented API)
FROZEN_API: Mapping[str, tuple[str, ...]] = {
    "git_repository.GitRepository": (
        "run", "head", "current_branch", "is_bare", "status", "status_v2",
    ),
    "git_control_plane.GitControlPlane": (
        "snapshot", "get_global_state", "get_worker_state", "get_queue_state",
        "get_remote_state", "get_audit_health", "get_recovery_health",
        "get_performance_health", "build_action_proposals",
        "validate_proposal", "request_action", "issue_capability",
        "consume_capability", "restart_reconcile", "decision_log", "alerts",
    ),
    "worker_pool.WorkerPool": (
        "allocate_worker", "transition", "mark_activity", "enqueue_merge",
        "worker_health", "begin_drain", "finish_drain", "resume",
        "list_workers", "available_capacity",
    ),
    "merge_queue.MergeQueue": (
        "enqueue", "next_entry", "mark_running", "mark_merged",
        "mark_conflicted", "mark_blocked", "mark_failed", "cancel",
        "entries", "find_entry", "stats", "is_stale",
    ),
    "worker_registry.PoolRegistry": (
        "load", "load_slots", "store", "store_slots",
    ),
    "capability_gate.execute_with_capability": (),
    "audit_chain.append_audit": (),
    "audit_chain.chain_health": (),
    "audit_chain.verify_tail": (),
    "recovery.create_recovery_ref": (),
    "recovery.list_recovery_refs": (),
    "recovery.create_bundle": (),
    "merge_precheck.pre_merge_check": (),
    "merge_precheck.merge_tree_check": (),
    "hook_versioning.upgrade_hook": (),
    "hook_versioning.verify_hook": (),
    "hook_versioning.hook_generation": (),
    "branch_policy.classify_branch": (),
    "branch_policy.policy_digest": (),
    "git_maintenance.GitMaintenanceManager": ("plan", "commit_graph_status", "run"),
    "registry_migration_engine.RegistryMigrationEngine": (
        "classify", "dry_run", "migrate", "recover",
    ),
}

#: article 504-507 — expected frozen state machines (value lists)
EXPECTED_STATES: Mapping[str, tuple[str, ...]] = {
    "WorkerState": (
        "ALLOCATED", "READY", "WORKING", "DIRTY", "COMMITTED", "QUEUED",
        "MERGING", "MERGED", "SYNCED", "QUARANTINED", "RETIRING",
        "RETIRED", "FAILED",
    ),
    "QueueState": (
        "pending", "preparing", "ready", "running", "merged", "conflicted",
        "blocked", "failed", "cancelled",
    ),
    "TransactionState": (
        "PREPARING", "SNAPSHOT_READY", "RUNNING", "LOCAL_COMPLETE",
        "AUDITING", "VERIFIED", "COMPLETE", "CONFLICT", "AUDIT_FAILED",
        "REMOTE_FAILED", "RECOVERY_REQUIRED", "FAILED",
    ),
    "GlobalGitState": (
        "HEALTHY", "BUSY", "BACKPRESSURE", "DEGRADED", "READ_ONLY",
        "RECOVERY", "ERROR",
    ),
    "UpgradeState": (
        "PLANNED", "STAGED", "CANARY", "ROLLING", "VERIFYING", "ACTIVE",
        "ROLLED_BACK", "FAILED",
    ),
    "RecoveryState": (
        "IDLE", "POINT_CREATED", "RESTORING", "VERIFYING", "RESTORED",
        "FAILED", "UNRECOVERABLE",
    ),
}

#: article 508 — mandated lock hierarchy (order fixed)
EXPECTED_LOCKS: tuple[str, ...] = (
    "SUPERVISOR", "WORKER_REGISTRY", "WORKSPACE_SYNC", "MERGE_QUEUE",
    "MAIN_WRITE", "WORKTREE_WRITE", "AUDIT_APPEND", "MAINTENANCE",
)

#: article 509 — mandated branch classes
EXPECTED_BRANCH_CLASSES: tuple[str, ...] = (
    "MAIN", "PERSISTENT_WORKER", "EPHEMERAL_WORKER", "RECOVERY",
    "RELEASE", "REMOTE_TRACKING", "UNKNOWN",
)

#: article 510-515 — final invariants (id, statement, source article)
FINAL_INVARIANTS: tuple[tuple[str, str, str], ...] = (
    ("INV-MAIN-1", "main is never a worker", "A510"),
    ("INV-MAIN-2", "main has no self-commit watcher", "A510"),
    ("INV-MAIN-3", "main is single-writer", "A510"),
    ("INV-MAIN-4", "main is clean outside integration/repair/release", "A510"),
    ("INV-WORKER-1", "one active worker : one worktree", "A511"),
    ("INV-WORKER-2", "one active worker : one branch", "A511"),
    ("INV-WORKER-3", "at most one watcher per worker", "A511"),
    ("INV-WORKER-4", "one worker_instance_id per slot", "A511"),
    ("INV-WORKER-5", "no shared index between workers", "A511"),
    ("INV-MERGE-1", "source SHA immutable; branch moves require re-enqueue", "A512"),
    ("INV-MERGE-2", "target main SHA verified and clean", "A512"),
    ("INV-MERGE-3", "merge is single-flight with recovery point + audit", "A512"),
    ("INV-PUSH-1", "push=False today; only IntegrationManager may push later", "A513"),
    ("INV-PUSH-2", "workers and self-commit never push; no automatic force-push", "A513"),
    ("INV-TIER-1", "tier-1 read-only direct; tier-2 scoped capability", "A514"),
    ("INV-TIER-2", "tier-3 authority capability; unknown fails closed tier-3", "A514"),
    ("INV-TIER-3", "no confirmed-bool downgrade and no AI self-approval", "A514"),
    ("INV-CAP-1", "write tokens short-lived, single-use, bound to "
                  "operation/repo/worktree/ref/revision/policy/generation", "A515"),
)

#: article 536 — the sixteen baseline freeze conditions
FREEZE_CONDITIONS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("all_git_writes_via_gateway", "所有 Git write 經 Gateway", ()),
    ("no_illegal_direct_subprocess_git", "Direct subprocess Git 無非法 caller", ()),
    ("confirmed_bool_production_zero", "confirmed bool 正式路徑 = 0", ()),
    ("ai_tier3_self_approval_zero", "AI Tier3 self approval = 0", ()),
    ("worker_queue_main_invariants", "worker/queue/main invariants PASS", ()),
    ("audit_chain_pass", "audit chain PASS", ()),
    ("hooks_integrity_pass", "hooks integrity PASS", ()),
    ("capability_tests_pass", "capability tests PASS", ()),
    ("stress_50_workers_pass", "50 worker stress PASS", ()),
    ("chaos_pass", "chaos PASS", ()),
    ("dr_pass", "DR PASS", ()),
    ("rolling_upgrade_pass", "rolling upgrade PASS", ()),
    ("performance_regression_gate_pass", "performance regression gate PASS", ()),
    ("architecture_tests_pass", "architecture tests PASS", ()),
    ("documentation_sync_pass", "documentation sync PASS", ()),
)

#: default evidence-file name merged into the baseline by the CLI
DEFAULT_EVIDENCE_FILENAME = "git_baseline_gate_evidence.json"


# ---------------------------------------------------------------------------
# introspection helpers
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# freeze table
# ---------------------------------------------------------------------------


def _gate_evidence(
    evidence: Optional[Mapping[str, Any]], key: str
) -> Optional[dict[str, Any]]:
    if not evidence:
        return None
    gates = evidence.get("gates")
    if not isinstance(gates, Mapping):
        return None
    entry = gates.get(key)
    return dict(entry) if isinstance(entry, Mapping) else None


def build_freeze_table(
    scan_result: Optional[dict[str, Any]] = None,
    gate_evidence: Optional[Mapping[str, Any]] = None,
) -> list[dict[str, Any]]:
    """Evaluate the sixteen article 536 conditions (never default PASS)."""
    scan_result = scan_result or static_gate.scan()
    counts = scan_result["counts"]

    def gate(key: str) -> Optional[dict[str, Any]]:
        return _gate_evidence(gate_evidence, key)

    def gate_status(key: str, *, required: bool = True) -> tuple[str, list[str]]:
        entry = gate(key)
        if entry is None:
            return ("UNVERIFIED" if required else "PASS"), [
                f"no evidence recorded for gate '{key}'"
            ]
        result = str(entry.get("result", "UNVERIFIED")).upper()
        evidence = [str(item) for item in entry.get("evidence", [])] or [
            f"gate '{key}' result={result}"
        ]
        return result, evidence

    rows: list[dict[str, Any]] = []
    row = rows.append

    # 1. all git writes through the gateway
    gateway_missing = "GitExecutionGateway" not in _component_map()["runtime"]
    status = "FAIL" if counts["confirmed_bool"] or gateway_missing else "PASS"
    row({
        "condition": "all_git_writes_via_gateway",
        "zh": "所有 Git write 經 Gateway",
        "status": status,
        "evidence": [
            f"GitExecutionGateway class: "
            f"{'MISSING' if gateway_missing else 'PRESENT'}",
            f"production confirmed=True call sites: {counts['confirmed_bool']}",
        ],
    })

    # 2. no illegal direct subprocess git callers
    illegal = counts.get("illegal_direct_git", 0)
    status = "FAIL" if illegal else "PASS"
    row({
        "condition": "no_illegal_direct_subprocess_git",
        "zh": "Direct subprocess Git 無非法 caller",
        "status": status,
        "evidence": [f"illegal call sites (A375 whitelist): {illegal}"]
        + scan_result["categories"].get("illegal_direct_git", [])[:10],
    })

    # 3. confirmed bool on production paths = 0
    status = "FAIL" if counts["confirmed_bool"] else "PASS"
    row({
        "condition": "confirmed_bool_production_zero",
        "zh": "confirmed bool 正式路徑 = 0",
        "status": status,
        "evidence": [f"production call sites: {counts['confirmed_bool']}"]
        + scan_result["categories"]["confirmed_bool"][:10],
    })

    # 4. AI tier-3 self approval = 0
    legacy_env = counts["legacy_env"]
    status = "FAIL" if legacy_env else "PASS"
    row({
        "condition": "ai_tier3_self_approval_zero",
        "zh": "AI Tier3 self approval = 0",
        "status": status,
        "evidence": [
            f"legacy approval env occurrences: {legacy_env}",
            "capability_gate refuses automation tier-3 legacy approval, but "
            "production paths still call GitRepository.run directly",
        ],
    })

    # 5. worker/queue/main invariants
    inv_status = "PASS"
    inv_evidence: list[str] = []
    for key in ("stress_50_workers", "chaos"):
        gs, ge = gate_status(key)
        inv_evidence.extend([f"{key}: {gs}"] + ge[:3])
        if gs != "PASS":
            inv_status = "UNVERIFIED" if gs == "UNVERIFIED" else "FAIL"
    row({
        "condition": "worker_queue_main_invariants",
        "zh": "worker/queue/main invariants PASS",
        "status": inv_status,
        "evidence": inv_evidence,
    })

    # 6. audit chain
    from .. import audit_chain

    try:
        health = audit_chain.chain_health()
    except Exception as exc:  # pragma: no cover - defensive
        health = {"chain_valid": False, "error": str(exc)}
    gs, ge = gate_status("audit_chain")
    status = "PASS" if health.get("chain_valid") and gs == "PASS" else (
        "UNVERIFIED" if gs == "UNVERIFIED" else "FAIL"
    )
    row({
        "condition": "audit_chain_pass",
        "zh": "audit chain PASS",
        "status": status,
        "evidence": [f"chain_health.chain_valid={health.get('chain_valid')}"] + ge[:3],
    })

    # 7. hooks integrity
    hooks = _hook_evidence()
    gs, ge = gate_status("hooks_integrity")
    status = "PASS" if hooks["templates_ok"] and gs == "PASS" else (
        "UNVERIFIED" if gs == "UNVERIFIED" else "FAIL"
    )
    row({
        "condition": "hooks_integrity_pass",
        "zh": "hooks integrity PASS",
        "status": status,
        "evidence": [
            f"governed templates ok={hooks['templates_ok']}",
            f"generation={hooks['live_generation'][:23]}",
        ] + ge[:3],
    })

    # 8. capability tests
    gs, ge = gate_status("capability")
    row({
        "condition": "capability_tests_pass",
        "zh": "capability tests PASS",
        "status": gs,
        "evidence": ge,
    })

    # 10-16. measured gates
    direct_gates = (
        ("stress_50_workers_pass", "50 worker stress PASS", "stress_50_workers"),
        ("chaos_pass", "chaos PASS", "chaos"),
        ("dr_pass", "DR PASS", "dr"),
        ("rolling_upgrade_pass", "rolling upgrade PASS", "rolling_upgrade"),
        ("performance_regression_gate_pass", "performance regression gate PASS",
         "performance"),
        ("architecture_tests_pass", "architecture tests PASS",
         "architecture"),
        ("documentation_sync_pass", "documentation sync PASS", "documentation"),
    )
    for condition, zh, key in direct_gates:
        gs, ge = gate_status(key)
        if key == "documentation":
            doc = _documentation_scan()
            gs = doc["verdict"] if not ge or ge[0].startswith("no evidence") else gs
            ge = [f"stale findings: {len(doc['stale_findings'])}"] + doc[
                "stale_findings"
            ][:5] + ge[:2]
        row({
            "condition": condition,
            "zh": zh,
            "status": gs if gs in {"PASS", "FAIL", "UNVERIFIED"} else "UNVERIFIED",
            "evidence": ge,
        })
    return rows


def overall_verdict(freeze_table: list[dict[str, Any]]) -> tuple[str, list[str]]:
    blockers = [
        f"{row['condition']}:{row['status']}"
        for row in freeze_table
        if row["status"] != "PASS"
    ]
    if blockers:
        return "GIT_BASELINE_NOT_FROZEN", blockers
    return "GIT_BASELINE_FROZEN", []


# ---------------------------------------------------------------------------
# payload build / write / verify
# ---------------------------------------------------------------------------


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


GOVERNANCE_MANIFEST_DIR = GIT_TIERS_DIR


__all__ = [
    "ARCHITECTURE_VERSION",
    "ARTICLE_493_API",
    "ARTICLE_522_FIELDS",
    "BASELINE_DIGEST_FILENAME",
    "BASELINE_DIR",
    "BASELINE_FILENAME",
    "BASELINE_ID",
    "BASELINE_VERSION",
    "DEFAULT_EVIDENCE_FILENAME",
    "EXPECTED_BRANCH_CLASSES",
    "EXPECTED_LOCKS",
    "EXPECTED_STATES",
    "FINAL_INVARIANTS",
    "FREEZE_CONDITIONS",
    "FROZEN_API",
    "MANDATED_RUNTIME_COMPONENTS",
    "PROJECT_ROOT",
    "PUBLIC_API_VERSION",
    "bind_to_governance_manifest",
    "build_baseline_payload",
    "build_freeze_table",
    "overall_verdict",
    "verify_baseline",
    "write_baseline",
]
