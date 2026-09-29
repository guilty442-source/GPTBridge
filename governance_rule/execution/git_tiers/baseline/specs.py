"""Golden Git baseline spec tables (A185 split).

Extracted from ``baseline/__init__`` (source-size contract): owns the
baseline identity constants, the article-522 field list, the mandated
component/driver/support sets (article 489), the frozen API surface
(article 493), the expected state machines (article 504-507), the lock
hierarchy (article 508), branch classes (article 509), the final
invariants (article 510-515) and the sixteen freeze conditions
(article 536).  Pure data — no I/O.
"""
from __future__ import annotations

from pathlib import Path
from typing import Mapping

from .. import governance_manifest

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

GOVERNANCE_MANIFEST_DIR = GIT_TIERS_DIR


__all__ = [
    "ARCHITECTURE_VERSION", "ARTICLE_493_API", "ARTICLE_522_FIELDS",
    "BASELINE_DIGEST_FILENAME", "BASELINE_DIR", "BASELINE_FILENAME",
    "BASELINE_ID", "BASELINE_VERSION", "DEFAULT_EVIDENCE_FILENAME",
    "EXPECTED_BRANCH_CLASSES", "EXPECTED_LOCKS", "EXPECTED_STATES",
    "FINAL_INVARIANTS", "FREEZE_CONDITIONS", "FROZEN_API",
    "GOVERNANCE_MANIFEST_DIR", "GIT_TIERS_DIR", "INTEGRITY_FIELD",
    "MANDATED_DRIVER", "MANDATED_RUNTIME_COMPONENTS", "MANDATED_SUPPORT",
    "PROJECT_ROOT", "PUBLIC_API_VERSION",
]
