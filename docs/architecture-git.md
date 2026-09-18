# GPTBridge Git Architecture — Final Baseline

Per codex A510–A515 / A522–A539 (golden Git baseline v1) and the
centralized, layered, recoverable local version service, the repository uses
Git worktrees as the layering mechanism. This document is the final
operator contract for the Git plane.

Authoritative machine-readable topology lives in
`governance_rule/execution/git_tiers/baseline/gptbridge_git_baseline_v1.json`
and `governance_rule/execution/git_tiers/git_governance_manifest.json`. This
document references those sources instead of restating topology.

## 1. Worktree Roles

| Worktree | Branch | Role |
| --- | --- | --- |
| `E:\GPTBridge` | `main` | **Integration-only** checkout |
| `E:\GPTBridge\.worktrees\git` | `git` | Persistent worker |
| `E:\GPTBridge\.worktrees\local-model` | `local-model` | Persistent worker |
| `E:\GPTBridge\.worktrees\rag` | `rag` | Persistent worker |
| `E:\GPTBridge\.worktrees\ui` | `ui` | Persistent worker |
| ephemeral AI worktrees (`ai/`, `arch-`, `bright-`, `checker-`, `flossy-`, `sandy-`, `permission-`, `runtime-`, `sync-`, `xingcheng-` …) | ephemeral branches | Ephemeral workers (`worker_pool` `wNNN` slots) |

### 1.1 main is integration-only

- `main` is **not** a worker (INV-MAIN-1).
- `main` has **no self-commit watcher** (INV-MAIN-2). The automation
  supervisor explicitly excludes the main checkout from watcher
  supervision (`automation_supervisor_loop._refresh_watchers`).
- `main` is **single-writer** (INV-MAIN-3): it accepts only the serialized
  integration operations performed by the IntegrationManager / Coordinator
  (merge, fast-forward after merge, repair, release). No worker ever writes
  `main` directly.
- `main` is **clean** outside integration/repair/release (INV-MAIN-4).

### 1.2 Worker sync sources

Workers are the only sources synchronized into `main`:

- `git`
- `local-model`
- `rag`
- `ui`
- ephemeral AI worktrees

Persistent workers (`git`, `local-model`, `rag`, `ui`) are protected: never
auto-deleted; may be auto fast-forwarded by the coordinator after
integration. Ephemeral branches may be proposed for retirement after their
lifecycle completes; actual deletion always stays a Tier-3 governed
operation.

Invariants (INV-WORKER): one active worker ⇔ one worktree; one active
worker ⇔ one branch; at most one watcher per worker; one
`worker_instance_id` per slot; no shared index between workers.

## 2. High-Level Flow

```text
Worker Worktrees (git / local-model / rag / ui / ephemeral AI)
        │  changes land inside the worker checkout
        ▼
Self-Commit (one watcher per worker; commits only, never pushes)
        │  source SHA is recorded at enqueue
        ▼
SHA-pinned Merge Queue (source SHA immutable; branch moves need re-enqueue)
        │
        ▼
Precheck (target main SHA verified and clean)
        │
        ▼
Recovery Point (recovery ref / bundle before the merge)
        │
        ▼
Single-flight Merge to main (only the IntegrationManager merges; one at a time)
        │
        ▼
Post-merge Governance Audit (chained audit records)
        │
        ▼
Verification (verdict recorded; failure stops the cycle)
        │
        ▼
Fast-forward clean worker worktrees
        │
        ▼
Coordinator-only push to origin  (currently push=False)
```

## 3. Safety Principles (unchanged)

- **Conflict ⇒ stop.** A conflicted merge is reported and the cycle stops;
  the coordinator never chooses a conflict resolution.
- **Workers never push.** Self-commit and workers only commit. Only the
  IntegrationManager / Coordinator may push, and only after full
  integration + audit + verification.
- **No automatic force-push.**
- **No automatic ref deletion.**
- **No automatic conflict resolution.**
- **Timeouts fail closed.** Startup 10 s, forced test suite 20 s, isolated
  audit flow 30 s; on timeout the operation is treated as failed, never
  assumed successful.

## 4. Formal Core Components

The runtime plane is exactly **six** mandated managers/services plus **one**
driver. No seventh runtime manager/service/coordinator is added.

| Component | Kind | Implementation |
| --- | --- | --- |
| `GitControlPlane` | runtime manager | `git_control_plane.GitControlPlane` — global state, health, worker/queue/remote state, capabilities, decision log |
| `WorkerManager` | runtime manager | `worker_pool.WorkerPool` + `worker_registry.PoolRegistry` + `worker_lifecycle` / `worker_reconcile` (single worker registry owner) |
| `IntegrationManager` | runtime manager | `coordinator.Coordinator` + `merge_queue.MergeQueue` + `merge_precheck` + `workspace_sync` (single queue owner, single-flight merge) |
| `GitExecutionGateway` | runtime gateway | `capability_gate.execute_with_capability` (scoped Tier-2 / authority Tier-3) — every Git write enters through this gateway |
| `EvidenceManager` | runtime manager | `audit_chain` + `audit_records` + `snapshot` + `recovery` (single audit writer, chained append, recovery refs/bundles) |
| `MaintenanceManager` | runtime manager | `git_maintenance.GitMaintenanceManager` |
| `GitProcessDriver` | single driver | `git_repository.GitRepository.run` — the only subprocess Git driver (status parsing is centralized in `porcelain`) |

Support modules (single instances, per article 489): `GitCommandNormalizer`
(`command_normalizer`), `RepositoryObserver` (`porcelain.status_v2` +
control-plane collectors), `BranchPolicy` (`branch_policy`, single branch
policy source), `GitRuntimeConfig` (`worker_pool_types.PoolConfig` +
governance-manifest timings), `LockManager` (`locks.LOCK_ORDER` +
`process_lock.ProcessFileLock`, single lock model),
`CapabilityVerifier` (`capability_verify`), `GitInvariants`
(`fault_injection`).

## 5. Merge Discipline

- Merge Queue entries are bound to the **source SHA** (INV-MERGE-1): the
  SHA is immutable; a moving branch requires re-enqueue.
- Target `main` SHA is verified and clean before merging (INV-MERGE-2).
- A merge is **single-flight**: exactly one merge runs at a time, guarded
  by a recovery point + chained audit (INV-MERGE-3).
- After the merge: governance audit → verification → fast-forward of clean
  worker worktrees.

## 6. Origin Mirror

| Remote | URL | Role |
| --- | --- | --- |
| `origin` | `github.com/guilty442-source/GPTBridge.git` | GitHub mirror |

Two revisions are always recorded explicitly and never assumed equal:
`local_main_sha` (integration worktree) and `origin_main_sha` (remote
tracking). Sync states: `IN_SYNC | LOCAL_AHEAD | ORIGIN_AHEAD | DIVERGED |
MISSING_REF`. `DIVERGED` never triggers automatic merge, reset or
force-push — it is reported and the cycle stops.

**Currently `push=False`** (INV-PUSH-1/2): workers and self-commit never
push; no automatic force-push; only the IntegrationManager may push later,
and only through a governed change.

## 7. Hooks

- `pre-commit` — whitespace check (`git diff --cached --check`) + audit.
- `pre-merge-commit` — same as pre-commit.
- `pre-push` — blocks force-push / ref deletion / non-fast-forward unless
  `GOVERNANCE_AUTHORITY_APPROVAL=1`.

Versions and digests are governed by `hook_versioning`
(`verify_hook` / `upgrade_hook` / `hook_generation`).

## 8. Recovery

Before every merge a recovery point is created. Recovery capability
(`recovery.py`) provides `create_recovery_ref` / `list_recovery_refs` /
`create_bundle`; a bundle is verified before it is used. Recovery events are
linked into the chained audit. Registry recovery is covered by
`registry_migration_engine.RegistryMigrationEngine` (`recover`).

## 9. Frozen Contract

The final baseline freeze derives strictly from codex article 536 — sixteen
conditions, none defaulting to PASS. The current verdict is computed by
`baseline.overall_verdict` from measured gate evidence
(`git_baseline_gate_evidence.json`). Rust-gate CLI:
`python -m governance_rule.execution.git_tiers.baseline`.

Post-freeze changes are restricted to
`BUGFIX / SECURITY_FIX / PERFORMANCE_OPTIMIZATION / TEST_IMPROVEMENT /
DOCUMENTATION / VERSIONED_MIGRATION`; forbidden without re-baseline:
a new manager, coordinator, Git driver, audit ledger, lock system, state
machine, parallel queue/registry, or another Git gateway (article 539).