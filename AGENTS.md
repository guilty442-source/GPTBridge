# AGENTS.md — Project Guidelines for AI Workers

## Shell Environment

This project runs on **Windows PowerShell**. Bash-specific syntax does NOT work.

### Git Commits

**Do NOT use heredoc syntax** for commit messages. PowerShell does not support
`$(cat <<'EOF'...EOF)`.

**Correct method**: write the commit message to a temporary file, then use `-F`:

```powershell
# 1. Write message to a temp file
Set-Content -Path _commit_msg.txt -Value @"
Commit title here

Body text here.

Generated with [Devin](https://devin.ai)

Co-Authored-By: Devin <158243242+devin-ai-integration[bot]@users.noreply.github.com>
"@

# 2. Commit using -F
git commit -F _commit_msg.txt

# 3. Clean up
Remove-Item _commit_msg.txt -Force
```

**Alternative**: use a single-line message:

```powershell
git commit -m "Single-line commit message"
```

**Scope every commit to explicit paths** so externally staged work is never
swept into your commit (incident 2026-09-20: `ef58c9cc` carried another
worker's pre-staged P0 changes under a blueprint message):

```powershell
git add <your files>; git diff --cached --name-only   # verify only your files
git commit -F _commit_msg.txt
```

If the index already contains files you did not stage, never commit the whole
index. A verification listing is not enough — you must then either unstage the
foreign files (`git restore --staged <path>`) or commit path-scoped:

```powershell
git commit -m "Your message" -- <your-file-1> <your-file-2>
```

Path-scoped commits ignore the index for every other path, so externally
staged work can never be swept in (incident 2026-09-20: `4914cf07` swept a
staged `pretrain.py` CUDA-graphs fix under a blueprint message because the
whole index was committed after the listing was noticed).

### Other PowerShell Notes

- `ls -la` → use `Get-ChildItem` or `dir`
- `rm -rf` → use `Remove-Item -Recurse -Force`
- `2>&1` works differently; pipe errors with `2>&1` at the end of the command
- `find` → use `Get-ChildItem -Recurse -Filter`
- `grep` → use `Select-String`

## Git Hooks

> Normative authority: Codex A53/A245/A375（A375 為 Git 流程單一控制條）。本節為操作手冊，數值與規則以法典為準。

- **pre-commit**: runs `git diff --cached --check` (whitespace check) + audit log
- **pre-push**: blocks force-push / ref deletion / non-fast-forward unless `GOVERNANCE_AUTHORITY_APPROVAL=1`
- **pre-merge-commit**: same as pre-commit

Hooks are installed in `.git/hooks/` and shared across all worktrees.

## Worktrees

| Worktree | Path | Branch |
| --- | --- | --- |
| Main | `E:\GPTBridge` | `main` |
| Git | `E:\GPTBridge\.worktrees\git` | `git` |
| Local Model | `E:\GPTBridge\.worktrees\local-model` | `local-model` |
| RAG | `E:\GPTBridge\.worktrees\rag` | `rag` |
| UI | `E:\GPTBridge\.worktrees\ui` | `ui` |

Worktrees share the same `.git` directory. Hooks, config, and objects are common.

## Automatic Self-Commit (per worktree)

> Normative authority: Codex A163/A375。

Each worktree can automatically commit the changes made inside its own checkout.
The service only commits — it **never pushes**.

```powershell
# One-shot (scheduler / on-demand), act on every worktree including main
& main-system\.venv\Scripts\python.exe scripts\git-auto-commit.py --all --once

# One-shot, single worktree
& main-system\.venv\Scripts\python.exe scripts\git-auto-commit.py --worktree E:\GPTBridge\.worktrees\ui --once

# Long-running watcher for one worktree (interval + stability debounce in seconds)
& main-system\.venv\Scripts\python.exe scripts\git-auto-commit.py --worktree E:\GPTBridge\.worktrees\ui --watch --interval 30 --debounce 60

# Spawn one background watcher per worktree (no console window)
& main-system\.venv\Scripts\python.exe scripts\git-auto-commit.py --all --watch
```

Guards: skipped while merge/rebase/cherry-pick/revert is in progress, when the
worktree is clean, and when git identity is missing. Honours `.gitignore`
(ignored paths are never staged). Commits are recorded in the audit ledger with
operation `auto-commit`. Implementation:
`governance_rule/execution/git_tiers/self_commit.py`.

## Automatic Worktree Synchronization

> Normative authority: Codex A163/A375。

Commit each checkout, merge worker branches into `main`, audit the integrated
result, then fast-forward all clean worktrees. Conflicts stop the cycle. Only
the coordinator may push `main`; it never force-pushes, deletes refs, resets,
or chooses a conflict resolution.

```powershell
& main-system\.venv\Scripts\python.exe scripts\git-worktree-sync.py --root E:\GPTBridge
& main-system\.venv\Scripts\python.exe scripts\git-worktree-sync.py --root E:\GPTBridge --watch --interval 60 --no-commit --push
```

Use `--no-commit` when the per-worktree auto-commit watchers are active, so the
sync coordinator never competes with them for the Git index.

Only the synchronization coordinator may push. It pushes `main` only after all
worktrees are clean, governance audits pass, integration succeeds, and
`origin/main` is an ancestor of local `main`. Workers and self-commit watchers
must never push directly.

## Git Automation (main-system task)

> Normative authority: Codex A163/A375。
> Tunables single source: `main-system/config/automation-flows.json`（`git-automation` flow）。

The old `automation_supervisor` process fleet (one watcher process per
worktree + periodic sync) is replaced by a single in-process main-system
task: `GitAutomationService`
(`main-system/src-core/tasks/git_automation.py`), started by the startup
executor in the normal-information phase (`app.git_automation`).

- **Commit sweep** every 60 s: runs `self_commit.run_once` per registered
  worktree, but only after the dirty fingerprint has been stable for a
  60 s debounce — same stability contract as the old watchers, zero extra
  processes. A worktree whose index already holds staged-but-uncommitted
  changes is **skipped** (`staged-index-present`) so a human/agent mid-commit
  is never swept into an auto-commit with an unrelated message.
- **Sync cycle** every 300 s: runs `workspace_sync.synchronize`
  (commit → merge worker branches into `main` → audit → fast-forward).
  Conflicts stop that cycle until a human resolves them.
- Locking, merge/rebase guards, audit recording and the no-push rule all
  stay in the governed `git_tiers` functions; the task only schedules.

State: `main-system/runtime/state/git-automation.json`. One-shot
verification (first sweep only debounces; real sync commits dirty
worktrees — run when the tree is in a state you want committed):

```python
from tasks.git_automation import GitAutomationService
svc = GitAutomationService(r"E:\GPTBridge")
await svc.run_once_cycle()   # one sweep + one sync
```

The legacy `scripts/git-supervisor.py` entry point still works but is no
longer the default path — prefer the in-process task.

## 星澄 Self-Learning & Automatic Upgrade

> Normative authority: Codex A554。
> Tunables single source: `Standalone tools/local-model/runtime/settings/self-learning.json`。

The native model learns from its own verified data and can upgrade itself
through the same governed pipeline used for manual training:

1. collect verified examples (`language_training_example`, active & quality
   gated) from every role database,
2. if the number of new examples since the last cycle reaches the policy
   threshold, export a `star-transformer-sft/v1` snapshot and register a
   training dataset,
3. queue and run a governed SFT job initialised from the lifecycle's active
   weights,
4. evaluate the resulting artifact against the policy's eval suites
   (`star-native-eval-dialogue-v1` perplexity/tps gate **plus**
   `star-capability-suite-v2` per-category regression gate — zh-TW/en/
   math/code/reading/multi_turn/context_tracking/instruction/
   tool_call_format/expert_routing; any category pass-rate drop vs the
   active weights fails the candidate) with the current active weights
   as baseline,
5. only if every gate passes: register the adapter, `stage`, and — when
   `auto_activate` is set — `activate`, register the weights in the model
   lifecycle, pin `runtime/settings/native-engine.json` to the new artifact
   and prune the previous generation (only the latest generation is kept).

Any failure is fail-closed: the active weights, the runtime checkpoint and
the adapter registry stay untouched. Policy: `runtime/settings/self-learning.json`
(`enabled=false` is the kill switch); state: `xingcheng/runtime/state/self-learning.json`;
reports: `xingcheng/runtime/logs/self-learning-*.json`.

### Scheduled operation (production path)

Self-learning is scheduled centrally through `AutomationCore` — the
`self-learning` flow in `main-system/config/automation-flows.json`
(`kind=periodic`, `interval_s=900`, `pausable=true`, `enabled` = manifest
kill switch). `SelfLearningDriver`
(`main-system/src-core/tasks/self_learning_driver.py`), started by the
startup executor, owns the cadence: each tick pre-checks the tool's
policy/state JSON (only to avoid waking a stopped tool for a cycle that
cannot run), wakes `local-model` through the governed
`ToolboxService.start_tool` path when cold (suppressed for 1 h after a
user-initiated stop, and while `worker_admission_hold`/`regulation_active`
are set), then submits `xingcheng_self_learning_cycle` via
`request_tool_execution` (queue-and-return — training is never run inside
the scheduler tick).

The cycle itself executes **inside the xingcheng tool process** through the
governed system channel — this is required because `inference_exclusion`
(§2.7-4) inspects the process-local engine caches (Python +
C++), which an external watcher cannot see. A re-entrant lock in the
service guarantees one cycle at a time; all policy gates (enabled,
min_new_examples, min_interval, quiet hours, GPU backoff, failure breaker,
daily budget, inference exclusion) are authoritatively enforced by
`run_cycle` in the tool, not duplicated in the driver. Responses are
drained on the next tick into a bounded ledger; driver state:
`main-system/runtime/state/self-learning-driver.json`.

Do NOT run `--watch` as production scheduling — it is a debugging aid only.
A denied registration (kill switch / unlisted flow) never falls back to a
private loop.

```powershell
# status / one-shot / force (ignore the new-example threshold) / kill switch
& main-system\.venv\Scripts\python.exe -m xingcheng.infrastructure.native_transformer.self_learning --status
& main-system\.venv\Scripts\python.exe -m xingcheng.infrastructure.native_transformer.self_learning --run-once
& main-system\.venv\Scripts\python.exe -m xingcheng.infrastructure.native_transformer.self_learning --run-once --force
& main-system\.venv\Scripts\python.exe -m xingcheng.infrastructure.native_transformer.self_learning --disable
```

Run from `Standalone tools\local-model\src\backend\services` (the package root).

Implementation: `native_transformer/self_learning.py` +
`native_transformer/self_learning_support.py` (cycle); driver:
`main-system/src-core/tasks/self_learning_driver.py`; tool handler:
`xingcheng/application/local_ai_lifecycle.py::_handle_self_learning`.

## 星澄 Model Maturity (`star-model-maturity/v1`)

> Normative authority: Codex A556/A557。

Unified maturity ladder; the certified level is decided **only by executed
tests** — parameter count is recorded as evidence, never a criterion.
Levels must pass consecutively; the first `fail`/`skipped` level caps the
certification.

| Level | Code | Gate |
| --- | --- | --- |
| 0 | `structure_init` | 結構初始化、參數全 finite |
| 1 | `forward_backward` | forward loss finite、全參數有梯度、optimizer step 後 logits finite |
| 2 | `overfit_small` | 固定 8 樣本過擬合：loss ≤ 0.5 或 ≤ 20% 初始值（測完還原權重） |
| 3 | `effective_pretrain` | held-out ppl ≤ 25% × vocab_size（對齊 random-uniform 基線） |
| 4 | `generation` | ≥75% 探針產生足量、多樣、非退化文本 |
| 5 | `dialogue_instruction` | ≥75% 對話探針通過（回合邊界、逐字複誦、限定回答、多輪記憶） |
| 6 | `reasoning_tools` | 可驗證算術/比較 + `<tool_call>` schema 合法，通過率 ≥50% 且 tool call 有效 |
| 7 | `controlled_evolution` | kill-switch fail-closed 實測 + lifecycle register/activate/rollback 實測 + 受管升級證據（self-learning 報告或 ≥2 代權重 + ≥1 評估報告） |

```powershell
# full ladder against a trained checkpoint
& main-system\.venv\Scripts\python.exe -m xingcheng.infrastructure.native_transformer.maturity --checkpoint <final.pt> --tool-root "Standalone tools\local-model" --device cpu --save

# architecture-only ladder (L0-L2; L3+ reports skipped)
& main-system\.venv\Scripts\python.exe -m xingcheng.infrastructure.native_transformer.maturity --preset small

# latest certified level
& main-system\.venv\Scripts\python.exe -m xingcheng.infrastructure.native_transformer.maturity --status --tool-root "Standalone tools\local-model"
```

Reports: `xingcheng/runtime/logs/maturity-*.json`; latest state:
`xingcheng/runtime/state/model-maturity.json`. Implementation:
`native_transformer/maturity.py` (`certify`, `current_maturity`,
`persist_report`).

## 星澄 Data Retention (`star-retention-policy/v1`)

> Normative authority: Codex A113/A114。
> Tunables single source: `Standalone tools/local-model/runtime/settings/retention.json`。

Bounds local-model runtime growth: old governed job dirs, logs, maturity /
self-learning reports and SFT snapshots are pruned by count and age.
**Never deletes** paths referenced by any `lifecycle.json` artifact version
or the checkpoint pinned in `runtime/settings/native-engine.json`
(unresolvable paths are fail-closed kept). Deletions append to
`xingcheng/runtime/logs/retention.jsonl`. Policy:
`runtime/settings/retention.json` (`enabled=false` disables everything).
Scheduled operation: the `retention` flow (`kind=periodic`, `interval_s=3600`)
is registered by `SelfLearningDriver` (`main-system/src-core/tasks/self_learning_driver.py`)
through `AutomationCore`. Each tick submits `xingcheng_retention_sweep` via the
governed system channel **only when the xingcheng tool is already running** —
opportunistic execution; a cold tool is never woken just to prune files
(deferred, not lost). It also still runs at the end of every self-learning
cycle, so this periodic flow exists to prevent retention starvation when
self-learning is disabled. Kill switches: manifest `enabled=false` stops the
schedule; `retention.json` `enabled=false` stops deletion. Manual:

```powershell
# dry-run (default) / apply / status
& main-system\.venv\Scripts\python.exe -m xingcheng.infrastructure.native_transformer.retention --tool-root "Standalone tools\local-model"
& main-system\.venv\Scripts\python.exe -m xingcheng.infrastructure.native_transformer.retention --tool-root "Standalone tools\local-model" --apply
& main-system\.venv\Scripts\python.exe -m xingcheng.infrastructure.native_transformer.retention --tool-root "Standalone tools\local-model" --status
```

Implementation: `native_transformer/retention.py` (`apply_retention`).

## 星澄 Training GPU Gate & Auto-Release

> Normative authority: Codex A239/A116。
> Tunables single source: `Standalone tools/local-model/runtime/settings/native-engine.json`＋bounded config keys（`gpu_required_mb`／`gpu_acquire_timeout_s`／`auto_release_idle_seconds`）。

- `TrainingJobExecutor.run_job` gates CUDA training through
  `shared_layer.adaptive.gpu_coordinator` before starting: jobs wait for
  `gpu_required_mb` free VRAM (default 2500, bounded config keys
  `gpu_required_mb` / `gpu_acquire_timeout_s`); timeout fails the job
  `EXECUTOR_GPU_BUSY` (fail-closed, no OOM contention). Only the real
  trainer is gated — injected `train_fn` stubs skip it. Completed jobs
  register a new lifecycle weights version but **never auto-activate**;
  promotion only happens through the eval-gated path (self-learning) or
  explicit approval.
- `native_engine.native_engine_for` registers cached engines with
  `execution/auto_release.py` `AutoReleaseManager`: idle timeout
  (`settings.auto_release_idle_seconds`, default 300 s) or memory
  pressure evicts the engine from cache; in-flight generation keeps its
  own strong reference and finishes normally.
- `NativeTransformerEngine` CUDA load also passes through the same
  coordinator: required VRAM is estimated from parameter count
  (bf16 ≈ 2 B/param × 1.5 headroom, floor 256 MB) and acquired with
  `XINGCHENG_GPU_ACQUIRE_TIMEOUT_S` (default 15 s). On timeout the
  engine degrades to CPU (`gpu_budget_downgraded=True`) and appends a
  `gpu-budget-downgrade` entry to `native-engine-executions.jsonl` —
  inference degrades instead of contending for VRAM.
- Chat-foundation SFT dataset production line:
  `infrastructure/chat_foundation_dataset.py`
  (`star-chat-foundation/v1`; deterministic seed, corpus replay mixing,
  maturity probe values excluded).

## Lazy RAG/CAG (MS1/MS2)

> Normative authority: Codex A586/A587。

RAG + CAG are capability-critical, not boot-critical. By default the
composition root does NOT import or construct them — measured import
baseline: 2.43 s / 1153 modules / 176 MB RSS → 0.66 s / 670 modules /
53 MB. First retrieval need must call `await app.ensure_rag_cag_started()`
(`core_system/app_lifecycle.py`): builds `RagRuntimeIntegration`, starts
it, then builds/starts `CAGIntegration` (CAG needs `rag_orchestrator`).
A lock serializes concurrent first-use; the call is idempotent.
`GPTBRIDGE_RAG_EAGER=1` restores the legacy eager construct+start during
boot. Acceptance tests: `main-system/tests/test_p0_lazy_lifecycle_handover.py`
(also covers the MS4 handover health gate in `boot_core_handover.py` —
`global-success` is only written after standby readiness + health probes
+ stability window; failures mark `failed-isolated`/`rolled-back` and
reactivate the previous generation).

## On-Demand Model Activation (Lazy 星澄)

> Normative authority: Codex A586。
> Tunables single source: `main-system/config/tool-isolation-policy.json`＋`sleep-policy.json`。

`model-dialogue` opens without the local model (governor directive 2026-09-17).
When a dialogue message is sent while the model owner (`local-model`, runtime
identity `xingcheng`) is not running:

- `model-dialogue` submits the infer request to the governed AI channel and
  reports `正在啟動星澄模型服務…` through send progress (activation window).
- `ModelServiceActivationBroker` (`main-system/src-core/tasks/model_service_activation.py`),
  started by the startup executor, detects the queued `ai -> xingcheng` request
  and starts `local-model` through the governed `ToolboxService.start_tool`
  path (background, audited, throttled with backoff; stale rows and expired
  deadlines are ignored).  Status: `main-system/runtime/state/model-service-activation.json`.
- The xingcheng runtime claims the request and answers with the user-selected
  or auto-routed model.
- Isolation budget: `main-system/config/tool-isolation-policy.json` must cover
  the physical tool id `local-model` (2048 MB) — the model runtime registers
  under that id, not only under `xingcheng`.

### 星澄法典診斷指令（唯讀）

| 指令（xingcheng） | 對話按鈕（model-dialogue） | 用途 |
| --- | --- | --- |
| `xingcheng_codex_alignment` | 法典 × 實作對齊 | architecture registry（法典 == registry == permission routes == module manifests == 實體目錄）、formal rules 對應、法典摘要 |
| `xingcheng_codex_mirror_check` | 法典 × 架構圖同步 | 中文鏡像（版本、身分集合、必要表、五段鏈、hash、汙染、replacement damage）＋`architecture-*.md` 缺陷／工具文件覆蓋缺口 |

- 實作：`Standalone tools/local-model/src/backend/services/xingcheng/application/codex_diagnostics.py`；
  架構文件檢查：`governance_rule/execution/audit/architecture_docs.py`（診斷用，尚未納入硬性 audit）。
- 路由：`tool_routes.py` 中 `(model-dialogue|star-chat) -> xingcheng` 已含兩指令（唯讀註冊檔已恢復 read-only）。
- model-dialogue 於送出前若 owner 未啟動，會先走懶啟動；報告以 zh-TW 摘要顯示於對話。

## Resource Governor

> Normative authority: Codex A30/A116/A593。

Adaptive CPU/memory governor that watches every process owned by the current
user and lowers resource pressure automatically: sustained CPU hogs get
`BELOW_NORMAL` priority, extreme hogs get their CPU affinity capped to half of
the logical CPUs, and large idle processes have their working set trimmed
(`EmptyWorkingSet`).  Actions revert after ~5 calm minutes.  Protected:
Windows system processes, security software (including the user's antivirus)
and the governor itself.

```powershell
# status / one-shot (dry-run first) / start / stop
& main-system\.venv\Scripts\python.exe scripts\resource-governor.py --status
& main-system\.venv\Scripts\python.exe scripts\resource-governor.py --once --dry-run
& main-system\.venv\Scripts\python.exe scripts\resource-governor.py --start
& main-system\.venv\Scripts\python.exe scripts\resource-governor.py --stop

# cross-reboot persistence (per-user Run key: no elevation needed)
& main-system\.venv\Scripts\python.exe scripts\resource-governor.py --install-logon
& main-system\.venv\Scripts\python.exe scripts\resource-governor.py --uninstall-logon
```

Tunables: `--interval` (default 20s), `--cpu-busy` (50% of one core),
`--cpu-extreme` (150%), `--mem-trim-mb` (1500), `--sustain` (3 samples),
`--no-affinity`.  Actions are logged to
`main-system/runtime/logs/resource-governor.jsonl`; the latest cycle snapshot
is in `main-system/runtime/state/resource-governor.json`.

Implementation: `scripts/resource-governor.py`.

## Adaptive SQL Layer

`shared-layer/src/shared_layer/adaptive/` is the bounded, pre-approved control
layer for the local data platform (admission control, dynamic pool/batch,
retry, per-domain breakers, maintenance scheduling, cost gate, SQLite
fallback and Qdrant budgets).  Every adaptive parameter moves only inside
`AdaptiveEnvelope` (pool 2–8, batch 50–500, reconcile workers 1–2).

Transport priority queue: submissions declare `priority_class`
(`critical` / `interactive` / `background` / `maintenance`), claim orders by
`priority_value` then FIFO and skips requests past `deadline_at`
(migration `058_transport_priority_queue.sql`, wired in `store.py` /
`store_async.py` and `database/query_allowlist.py`).

Call sites that want adaptive behaviour consult the shared plane:

```python
from shared_layer.adaptive import get_plane

plane = get_plane()
plane.observe(LoadSignals(pg_latency_ms=..., transport_backlog=...))
decision = plane.admit("reconcile", module_id="file-sorter", budget=budget)
decision = plane.admit_write("index", priority_class, signals)
retry = plane.retry_for(error, attempt)
batch, delay, reason = plane.plan_upserts(metadata_pending=..., pending_points=...)
```

The plane is opt-in and safe before any observation: with no signals it
answers ALLOW, so wiring a call site never changes behaviour until a signal
producer feeds `observe()`.  Tests: `shared-layer/tests/test_adaptive_control.py`.

## Access Control Plane

`shared-layer/src/shared_layer/security/` covers identity, connection,
credential, session, permission, rotation and revocation:

- DSN purpose separation (`runtime` / `reader` / `admin` / `backup`); the
  admin/backup DSN is unavailable inside a runtime context
  (`GPTBRIDGE_RUNTIME_CONTEXT=1`) and elevated credentials are hard-blocked
  from spawned tools (`toolbox_constants._NETWORK_ISOLATION_BLOCKED_ENV`).
- Short-term session identity: `SessionIdentity` + `apply_session_identity()`
  bind `gptbridge.actor_id/module_id/request_id/decision_id/correlation_id`
  transaction-locally via bound `set_config`.
- Credential metadata only in PostgreSQL (`gptbridge_security.credential`,
  migration `087_security_identity_control.sql`); plaintext is rejected by
  `assert_metadata_only`; fingerprints are HMAC-SHA256.
- Rotation with grace period (create → verify → switch → grace → revoke) and
  strict emergency revocation (disable → terminate → rotate → raise
  generation → audit).
- Security generation fence: sensitive writes fail closed under a stale
  `gptbridge.security_generation`; raise via
  `gptbridge_security.raise_security_generation(reason, actor)`.
- SQLite boundaries (path allowlist + process identity + locator scope +
  owner-only ACL) and mandatory Qdrant scoping (`require_scope`, module_id
  always required).
- Least-privilege certification: `least_privilege_report()` +
  `certification_errors()` (no SUPERUSER/CREATEDB/CREATEROLE/BYPASSRLS,
  no PUBLIC grants).
  Tests: `shared-layer/tests/test_security_control.py`.

## Cross-Engine Workflow (Saga)

`shared-layer/src/shared_layer/workflow/` makes one business operation across
PostgreSQL + SQLite + Qdrant + NTFS recoverable, re-runnable and verifiable —
without distributed transactions / 2PC:

- Single-engine work stays in an ACID transaction; multi-engine work runs as
  a Saga with PostgreSQL as the operation authority
  (`gptbridge_workflow.operation` / `operation_step`, migration
  `113_workflow_operation.sql`).
- Steps are idempotent and checkpoint-resumable; the fixed compensation
  table maps each step to rollback / compensate / invalidate / supersede /
  reconcile / append-only (audit appends are never rolled back).
- Timeouts are resolved by lookup + verify, not treated as failures;
  exhausted or unknown states end in `REQUIRES_RECONCILE` / `QUARANTINED`.
- Operations run under leases (`claimed_by` / `lease_until`) so a crashed
  worker does not leave `RUNNING` forever; long steps heartbeat, short SQL
  steps do not.
- Transactional outbox + inbox dedup (at-least-once + idempotent execution;
  never exactly-once claims). Central transport stays PostgreSQL; SQLite
  keeps only a module-private fallback that can never declare central
  completion.
- Publish barrier: `PREPARING → INDEXING → VERIFYING → READY`; only `READY`
  is readable, and cross-engine verdicts degrade to `DEGRADED` / `CONFLICT`
  instead of pretending success.
- Atomic file writes (`temp → fsync → hash → rename`), tombstone deletes,
  operation fingerprints (Python/SQL parity), and a guard that forbids
  cross-engine work inside an open PostgreSQL transaction.
  Tests: `shared-layer/tests/test_workflow_consistency.py`.

## Architecture Registry (single source of truth)

> Normative authority: Codex A201/A232/A281。

`governance_rule/execution/audit/architecture_registry.json` is the one
machine-readable topology authority: every component declares
`component_id / architectural_role / runtime_form / owner_sovereign /
owner_sub_sovereign / execution_identity / physical_path / lifecycle /
canonical / dependencies / information_channels`.

- `architectural_role` (governance / startup / decision / information / data /
  execution / model / development-maintenance …) is deliberately separate
  from `runtime_form` (python-process / electron-app / standalone-service /
  database / external-service …), so "directory", "tool", "module",
  "service" and "layer" can no longer conflict.
- Active sovereigns are `decision / permission / system-runtime /
  synchronization / xingcheng-domain`; retired ones (`maintenance`,
  `automation`) exist only as compatibility shims and may never own a
  component.
- The governance audit runs `check_architecture_registry`: Codex == registry
  == permission-directory routes == module manifests == sovereign ownership
  == physical directories, in one pass.  Drift is a failure, not a warning.
- Do not create a second copy of the topology: docs, manifests and code must
  reference this registry instead of restating it.

Tests: `governance_rule/tests/test_architecture_registry.py`.

## Planning documents

Blueprint planning files are retired. Do not create or restore blueprint,
roadmap, phased-plan, flow-plan, or equivalent planning documents. Current
work is governed directly by the Codex, registered contracts, and explicit
user instructions.

## Codex Amendment Pipeline (A382/A488)

> Normative authority: Codex A382/A488/A537/A538。
> Tunables single source: `main-system/config/automation-flows.json`（`codex-amendment-intake` flow）。

Staged request artifacts (`artifact=codex-amendment-request`, `authority=request-only`)
in the canonical intake dirs — `main-system/runtime/state/` and
`governance_rule/execution/audit/convergence/` (`codex-amendment-request-*.json`) —
are advanced by the periodic AutomationCore flow `codex-amendment-intake`
(`main-system/src-core/tasks/codex_amendment_intake.py`, interval 300 s):

```
scan → ledger.begin (lineage lock, one active request per predecessor)
     → build_successor (PG authority export → candidate sqlite + manifest)
     → run_five_sovereign_audit (unanimous receipts + certificate)
     → ready-for-governor   ← stop line; publication stays governor-invoked
```

- Xingcheng's receipt requires the governed web-search path: the driver
  submits `xingcheng_web_search` to a running `local-model` via
  `ToolboxService.request_tool_execution` (sync callable bridged through
  `run_coroutine_threadsafe`). When the tool is cold the audit **defers**
  (`successor-built`) — it never wakes the tool just to audit and never
  permanently rejects a request for a transient outage.
- Crashed audits (record stuck at `auditing`) are rewound to
  `successor-built` — a certificate only exists after a unanimous pass.
- Publication: `governance_rule/execution/codex_amendment_executor.py
  --request <req.json> --prepared <candidate.sqlite3> --staging <dir>
  --audit-result <audit.json> --apply` (governor only).
- CLI: `python -m governance_rule.execution.codex_amendment_driver
  --scan | --request <path> | --all`.

Tests: `governance_rule/tests/test_codex_amendment_driver.py`,
`main-system/tests/test_codex_amendment_intake_driver.py`.

## Governance

- Codex files (`governance_rule/codex/*.py`) are **read-only** — do not modify without explicit user approval.
- Governance audit must pass before commits: `python -m governance_rule.execution.audit`
- **Implementation precedence**: preserve a verified superior implementation
  and converge the Codex or registered contract through its governed process;
  never roll back superior tested behavior to match retired planning text.
- Model core must remain separate from network functionality.
- All external network access must go through governed tool paths.

## Verification Commands

```powershell
# Python syntax check
python -c "import ast; ast.parse(open('file.py', encoding='utf-8').read())"

# Full test suite (bounded workers from pytest.ini keep local load low;
# cache is written to the managed main-system temp directory)
main-system\.venv\Scripts\python.exe -m pytest -q

# Governance audit
python -m governance_rule.execution.audit
```

## Build Commands

- Python venv: `main-system\.venv\Scripts\python.exe`
- TypeScript: `npx tsc --noEmit` (in `main-system/`)
- Electron: `npm run build` (in `main-system/`)
