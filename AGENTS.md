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
worker's pre-staged P0 changes under an unrelated commit message):

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
staged `pretrain.py` CUDA-graphs fix under an unrelated planning message because the
whole index was committed after the listing was noticed). Recurrence
2026-09-28: `808a9895` swept another worker's 14 staged
`native/resource_governor` split files under a release-fixtures
message while their own files landed in `717aa5fa`; attribution was
corrected non-destructively via `git notes` on `808a9895`.

### Other PowerShell Notes

- `ls -la` → use `Get-ChildItem` or `dir`
- `rm -rf` → use `Remove-Item -Recurse -Force`
- `2>&1` works differently; pipe errors with `2>&1` at the end of the command
- `find` → use `Get-ChildItem -Recurse -Filter`
- `grep` → use `Select-String`

## Git Hooks

> Normative authority: Codex C11/C37/C66（C66 為 Git 流程單一控制條）。本節為操作手冊，數值與規則以法典為準。

- **pre-commit**: runs `git diff --cached --check` (whitespace check) + audit log
- **pre-push**: blocks force-push / ref deletion / non-fast-forward unless `GOVERNANCE_AUTHORITY_APPROVAL=1`
- **pre-merge-commit**: same as pre-commit

Hooks are installed in `.git/hooks/` and shared across all worktrees.

## Worktrees

| Worktree | Path | Branch |
| --- | --- | --- |
| Main | `E:\GPTBridge` | `main` |
| Devin | `E:\GPTBridge\.worktrees\devin` | `devin` |
| Git | `E:\GPTBridge\.worktrees\git` | `git` |
| Local Model | `E:\GPTBridge\.worktrees\local-model` | `local-model` |
| RAG | `E:\GPTBridge\.worktrees\rag` | `rag` |
| UI | `E:\GPTBridge\.worktrees\ui` | `ui` |

Worktrees share the same `.git` directory. Hooks, config, and objects are common.

## Automatic Self-Commit (per worktree)

> Normative authority: Codex C22/C66。

Each worktree can automatically commit the changes made inside its own checkout.
The service only commits — it **never pushes**.

```powershell
# One-shot debounced sweep across every registered worktree
& shared-layer\csharp\GPTBridge.GitAutomation\publish\GPTBridge.GitAutomation.exe --sweep --root E:\GPTBridge

# Long-running watcher (sweep + sync intervals in seconds)
& shared-layer\csharp\GPTBridge.GitAutomation\publish\GPTBridge.GitAutomation.exe --watch --root E:\GPTBridge --interval 30 --debounce 60
```

Guards: skipped while merge/rebase/cherry-pick/revert is in progress, when the
worktree is clean, and when git identity is missing. Honours `.gitignore`
(ignored paths are never staged). Commits are recorded in the audit ledger with
operation `auto-commit`. Implementation: `SelfCommit` inside the governed
C# host (the former `git_tiers/self_commit.py` Python lane is retired).

## Automatic Worktree Synchronization

> Normative authority: Codex C22/C66。

Commit each checkout, merge worker branches into `main`, audit the integrated
result, then fast-forward all clean worktrees. Conflicts stop the cycle. Only
the coordinator may push `main`; it never force-pushes, deletes refs, resets,
or chooses a conflict resolution.

```powershell
& shared-layer\csharp\GPTBridge.GitAutomation\publish\GPTBridge.GitAutomation.exe --sync --root E:\GPTBridge
& shared-layer\csharp\GPTBridge.GitAutomation\publish\GPTBridge.GitAutomation.exe --watch --root E:\GPTBridge --sync-interval 60 --no-commit --push
```

Use `--no-commit` when the per-worktree auto-commit watchers are active, so the
sync coordinator never competes with them for the Git index.

Only the synchronization coordinator may push. It pushes `main` only after all
worktrees are clean, governance audits pass, integration succeeds, and
`origin/main` is an ancestor of local `main`. Workers and self-commit watchers
must never push directly.

## Git Automation (main-system task)

> Normative authority: Codex C22/C66。
> Tunables single source: `main-system/config/automation-flows.json`（`git-automation` flow）。

The old `automation_supervisor` process fleet (one watcher process per
worktree + periodic sync) is replaced by the single governed host
`GPTBridge.GitAutomation.exe` (`shared-layer/csharp/GPTBridge.GitAutomation`,
published to `shared-layer/csharp/GPTBridge.GitAutomation/publish/`). The
former in-process `GitAutomationService`
(`main-system/src-core/tasks/git_automation.py`) and the
`scripts/git-*.py` entry points are retired with the Python lane.

Production residency: `gptbridge-backend` supervises the **unified**
automation host `GPTBridge.Automation.exe --watch`
(`shared-layer/csharp/GPTBridge.Automation`) — the `resident-core.json`
`periodic_scheduler`. One process hosts the git plane (same
sweep+sync loop, in-process via `Program.WatchService`), the codex
plane (`CodexAutomation.RunWatch`) and the permission plane
(`PermissionAutomation.RunWatch`); per-plane state files, kill
switches and single-instance locks (`codex-automation.lock` /
`permission-automation.lock` / `git-automation.lock`) are unchanged,
plus one top-level `automation-host.lock`. The standalone
`GPTBridge.GitAutomation.exe` stays the manual/one-shot CLI
(`--once`/`--sweep`/`--sync`/`--status` never take the lock; a
standalone `--watch` keeps ownership of just the git plane — the
unified host defers it and adopts it on release).

- **Commit sweep** every 60 s: `SelfCommit.RunOnce` per registered
  worktree, but only after the dirty-state marker has been stable for a
  60 s debounce — same stability contract as the old watchers, zero extra
  processes. A worktree whose index already holds staged-but-uncommitted
  changes is **skipped** (`staged-index-present`) so a human/agent mid-commit
  is never swept into an auto-commit with an unrelated message.
- **Sync cycle** every 300 s: commit → merge worker branches into `main` →
  audit → fast-forward. Conflicts stop that cycle until resolved.
- Locking, merge/rebase guards, audit recording and the no-push rule stay
  inside the governed host; the CLI only schedules (`--once`, `--sweep`,
  `--sync`, `--status`).

State: `main-system/runtime/state/git-automation.json`. One-shot
verification (first sweep only debounces; real sync commits dirty
worktrees — run when the tree is in a state you want committed):

```powershell
& shared-layer\csharp\GPTBridge.GitAutomation\publish\GPTBridge.GitAutomation.exe --once --root E:\GPTBridge
```

## Multi-Worker Division of Labor（分工執行守則）

Multiple agent sessions may share one worktree (e.g. two Devin sessions
on `.worktrees/devin`). The unit of ownership is the **file**, not the
worktree. Distilled from the 2026-10-02 capability-chain split
(Registry/Graph/Evidence by one worker, ArchitectureCapabilityBinding
by another):

1. **Claim before starting.** Check `.git/gptbridge-automation/claims/`
   for active claims; write your own
   `<worker>-<task>-<date>.json` listing the file paths you will touch
   plus a TTL. Delete the claim file when done.
2. **Prefer new files.** When dividing a task, take slices that are new
   modules/contracts (zero merge surface) over edits to files a sibling
   is actively changing.
3. **Never edit a live file.** Before editing, `git status` + `git log`
   in the target worktree — a file modified minutes ago under another
   worker's commits is theirs; find a disjoint slice or wait.
4. **Match the sibling's conventions.** Read their committed modules
   first (format strings, `Emit`/`Validate`/`Check` shape, error-code
   vocabulary, `XcPaths`/`CanonicalJson`/`ModelLifecycle` helpers) so
   the split lands as one coherent design, not two dialects.
5. **Auto-sweep attribution.** The commit sweep commits dirty files
   under a generic `auto-commit` message. Commit your files promptly
   and path-scoped; if a sweep lands your work under a generic message,
   annotate attribution non-destructively with `git notes` (precedent:
   `808a9895`).
6. **Generated artifacts resolve by regeneration.** Merge conflicts in
   derived projections (`audit_checks_manifest.json`, codex mirrors)
   are resolved by keeping the newest emission — merge `main` into your
   branch and take the newer side; never hand-merge generated content.
   Workers fix conflicts on their own branch; only the sync coordinator
   merges into `main`.
7. **dir-exists activation gates.** Commit-time audits check that
   codex-mandated `architecture_activation_states` target roots exist on
   disk — a missing registered dir blocks every commit in the repo, not
   just yours. Creating the registered directory satisfies the
   precondition (it is not activation evidence — the row's
   `INCOMPLETE_EVIDENCE` state stands until governed migration lands).

## 星澄 Self-Learning & Automatic Upgrade

> Normative authority: Codex D131。
> Native lane landed (B167/B38 successor): `GPTBridge.XingchengLearning`
> (`xc-learning.exe`, C#) at
> `xingcheng/src/backend/csharp/GPTBridge.XingchengLearning`
> — orchestration, governed interfaces and dataset/eval gates in C#; model
> execution stays in the native C++ lane (`xingcheng_trainer.exe` +
> `xc_modeltool.exe`), reached only through audited subprocesses.
> Production scheduling is unchanged — cycles run inside the
> xingcheng tool process via the governed system channel.
> Tunables single source: `xingcheng/xingcheng/runtime/settings/self-learning.json`。

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
   (`star-native-eval-dialogue-20260921-125054` perplexity/tps gate **plus**
   `star-capability-suite-20260925-084920` per-category regression gate — zh-TW/en/
   math/code/reading/multi_turn/context_tracking/instruction/
   tool_call_format/expert_routing; any category pass-rate drop vs the
   active weights fails the candidate) with the current active weights
   as baseline,
5. only if every gate passes: register the adapter, `stage`, and — when
   `auto_activate` is set — `activate`, register the weights in the model
   lifecycle, pin `xingcheng/xingcheng/runtime/settings/native-engine.json` to the new artifact
   and prune the previous generation (only the latest generation is kept).

Any failure is fail-closed: the active weights, the runtime checkpoint and
the adapter registry stay untouched. While the 300M maturation sequence is
active, a cycle whose SFT job cannot be admitted (no declared capability vs
sequence head) reports `action=sealed` and keeps the collected dataset
registered — no doomed job is queued and `consecutive_failures` is not
incremented. When the sequence is complete every capability is resolved and
the guard releases untagged governed SFT (the recorded completion order);
capability-declared jobs stay denied until a governor reopens a bounded
lane with `--maturation-reopen`. Each finished cycle also self-verifies
(`learning_verification`, `star-learning-verify/v1`): it re-checks the
evidence chain it just produced — trainer report sanity, `verdict_owner:
"fsharp"` on every evaluation, engine/F# verdict parity
(`engine_passed`/`engine_comparison` embedded in each comparison), the
lifecycle transition matching the recorded action (upgraded ⇒ version
advanced + runtime pinned; otherwise unchanged + unpinned) and dataset
registration. An `upgraded` action that fails verification takes the
governed rollback path; the latest result surfaces as `last_verification`
in the self-learning state. `curriculum_intent_map` scopes each cycle's
dataset by intent per course (`sft-refresh` currently prioritizes the
weakest measured capabilities — instruction/tool_call_format/math/
code/reading — plus forward-looking multi_turn/context_tracking intents,
excluding saturated `conversation` traffic). Policy: `xingcheng/xingcheng/runtime/settings/self-learning.json`
(`enabled=false` is the kill switch); state: `xingcheng/xingcheng/runtime/state/self-learning.json`;
reports: `xingcheng/xingcheng/runtime/logs/self-learning-*.json`.

### Scheduled operation (production path)

The scheduling mechanism lives **inside the tool body**:
`xc-learning.exe --schedule [--interval-s N]` (default 900 s) starts a
resident loop that paces the same governed `RunCycle` used by
`--run-once`. Single-instance arbitration is a lock file at
`xingcheng/xingcheng/runtime/state/self-learning-schedule.lock` — a second
`--schedule` exits 1 with `lock held`, so duplicate schedulers are
impossible. Each tick writes a `star-self-learning-schedule/v1`
heartbeat to `xingcheng/xingcheng/runtime/state/self-learning-schedule.json`
(pid, phase `draining`/`cycling`/`sleeping`, last action, error).

Tick order is **drain before cycle**: a `queued` job takes precedence
and is run through the same serial claim (`TryClaimTrainingJob`), then
a live-state job defers the tick, and only a free lane runs a new
cycle. `RunCycleImpl` itself also defers (`action=deferred`) when any
job is in flight or queued — a cycle can never pile up duplicate
queued rows while the serial lane is occupied. All policy gates
(kill switch, quiet hours, min_new_examples, failure breaker,
inference exclusion, governor quota, single training lane) stay
authoritative inside `RunCycle`; the loop only paces.

The `self-learning` flow in `main-system/config/automation-flows.json`
remains as a registry/document entry with `enabled=false` (core
`xingcheng-internal`). The former external `GPTBridge.Automation`
self-learning plane (`SelfLearningPlane.cs`) has been excised — the
unified host carries only the git/codex/permission planes; the sole
scheduler is `xc-learning.exe --schedule` inside the tool body, so a
second scheduler cannot exist.

```powershell
# resident scheduler (single instance; kill switch still applies)
& "xingcheng\src\backend\csharp\GPTBridge.XingchengLearning\publish\xc-learning.exe" --tool-root "xingcheng" --schedule
```

```powershell
# status / one-shot / force (ignore the new-example threshold) / kill switch
& "xingcheng\src\backend\csharp\GPTBridge.XingchengLearning\publish\xc-learning.exe" --tool-root "xingcheng" --status
& "xingcheng\src\backend\csharp\GPTBridge.XingchengLearning\publish\xc-learning.exe" --tool-root "xingcheng" --run-once
& "xingcheng\src\backend\csharp\GPTBridge.XingchengLearning\publish\xc-learning.exe" --tool-root "xingcheng" --run-once --force
& "xingcheng\src\backend\csharp\GPTBridge.XingchengLearning\publish\xc-learning.exe" --tool-root "xingcheng" --disable

# governed end-to-end smoke (scratch model; never touches the pinned bundle):
& "...\publish\xc-learning.exe" --tool-root "xingcheng" --self-test
```

Implementation: `GPTBridge.XingchengLearning` (C#) —
`SelfLearning.cs` (cycle + gates), `Collectors.cs` (role DB),
`SftDataset.cs` (SFT/DPO snapshot bridges), `Repository.cs`
(PostgreSQL `gptbridge_xingcheng` schema + audit chain),
`JobExecutor.cs` + `NativeTools.cs` (native subprocess lane),
`Evaluation.cs` (`xc_modeltool eval`/`capability` measurement +
recording), `Lifecycle.cs` (`star-model-lifecycle/v1`), `Retention.cs`;
native
execution: `infrastructure/native_transformer/training/xingcheng_trainer.exe`,
bridge: `infrastructure/native_transformer/tools/xc_modeltool.exe`.

Evaluation verdict ownership (codex B139/B132/B141): the native engine
only measures; the authoritative pass/fail verdict + comparison is
computed by the F# evaluator `xc-eval`
(`src/backend/fsharp/GPTBridge.XingchengEval`, contract
`star-fsharp-eval-verdict/v1`), invoked by `Evaluation.cs` after every
suite run. The engine's own `passed`/`comparison` fields are carried as
`engine_comparison` evidence only. Missing or failing `xc-eval.exe`
fails closed (`EVAL_OWNER_UNAVAILABLE`/`EVAL_VERDICT_FAILED` → recorded
passed=0), so promotion can never proceed without an F# verdict.
The retired Python `self_learning*.py`/`training_job_executor.py` are
interface documentation only — never execution.

## 星澄 Capability Architecture（capability unification directive §0-§106）

> Successor phase in force: **Authority Convergence directive
> §0-§103** (2026-10-02) — Capability × Resource × Native Runtime ×
> Data × Release. No new capability names, governance verbs, parallel
> runtimes, stores or governors (§1/§101); `xc-fused-1` stays the only
> production canonical architecture (§2). Canonical pipeline:
> Architecture → Capability → Learning → ResourceGrant → Acceleration
> → Training → Evaluation → Evidence → Lifecycle → Promotion (§102).
>
> Batch-1 landings (§100 第一批):
> `CapabilityResolver.cs` is the single canonical resolver (§80) —
> `Require` (alias→canonical id, `CAPABILITY_UNKNOWN` on unregistered
> names, §16), `Descriptor`, `Dependencies` (graph REQUIRES closure),
> `RegressionSuite` (§18 auto-derived), `EvalSurfaces`,
> `RuntimeProfile`, `PoolClass`, `Inspect`. Governance entries that
> take a capability name route through it — maturation verbs,
> `--failure-record`, `--failure-pool-status`, `--recovery-*`,
> `--capability-regression-suite`, `--capability-binding-check` and
> the job request path never carry a free string (§15/§63).
> `ResourceErrors.AuthorityMainSystemOnly`
> (`RESOURCE_AUTHORITY_MAIN_SYSTEM_ONLY`, §6) plus
> `ResourceGovernorClient.AssertClientWriteScope` enforce that
> xingcheng writes only into `resource-requests`/`resource-reports`/
> `resource-receipts` (and the client audit file) — governor state
> and grant files stay governor-owned.
>
> Batch-2 landings (§100 第二批, commit `c1fdc94df`): `InstructionRecovery`
> admission now resolves the plan capability through `CapabilityResolver`
> (alias accepted, unknown → `CAPABILITY_UNKNOWN`) and runs
> `ArchitectureCapabilityBinding.AdmissionCheck` over the canonical id —
> a missing binding on the capability or its REQUIRES closure blocks the
> lane with `CAPABILITY_ARCHITECTURE_INCOMPLETE` (§17/§69); the lane
> ledger records the graph-derived regression contract via
> `CapabilityResolver.RegressionSuite` (§18). `Evaluation.RunEvaluation`
> now appends `star-capability-evidence/v1` rows per evaluated category
> after a capability-suite run — canonical id, candidate, suite hash,
> baseline/result/regression, `contribution=model` (§21-§22).
> `ConvergenceGate` gained a critical `capability-delta` step comparing
> the freshly emitted registry against
> `state/capability-registry.promoted.json`; protected regression blocks
> promotion, and the baseline refreshes only when the gate allows (§26).
>
> Batch-3 landings (§100 第三批, commit `18517c1b9`):
> `ResourceErrors.GrantRequired` (`RESOURCE_GRANT_REQUIRED`, §10) —
> `PreflightResourceGate` names a null post-preflight grant explicitly
> instead of dereferencing it, and `--training-batch-plan --grant <file>`
> routes through `TrainingAcceleration.GrantBoundBatchPlan` so plan
> envelopes are grant-bound (effective VRAM = min(driver, grant), §41;
> over-grant → `ACCELERATION_PLAN_OVER_GRANT`, §43). Without `--grant`
> the planner emits `grant_bound=false` — planning verbs carry no
> production execution authority.
>
> Batch-4 landings (§100 第四批, commit `40f9ee0af`): the orphaned
> toolkit `cuda_rtlane.h` is retired in this worktree too (main-worktree
> retirement: `1a05cc74a`); `cuda-parity-all` now sweeps the §32
> training hot shapes (768×768 / 768×2048 / 2048×768 / 768×1024 /
> 768×8192) plus the trainer's fp32 lane (`xcuda_sgemm_f32` — the
> cuBLAS replacement), so the parity report is §37 promotion evidence.
>
> Batch-6 landing (§100 第六批): `TeacherCollect` is native-only —
> each scope's teacher spec resolves to a governed Xingcheng bundle
> ("self" = pinned native-engine checkpoint, else a boundary-checked
> bundle dir) and generates through `xc_modeltool serve` infer; no
> legitimate teacher → disabled, never an external fallback (§57-§59).
> B154's Ollama registration row still needs the governed amendment
> before it is unregistered.
>
> Batch-7 landings (§100 第七批, commit `fe8eea849`): the release gate
> gained two critical steps — `native-dependency` (§73 blocking
> findings + driver-only CUDA contract) and `resource-contract` (§75:
> every usage receipt's grant_id must resolve to a governor-issued
> grant file).
>
> **Decision reversal (2026-10-02):** the xstore metadata-authority
> takeover is CANCELLED — PostgreSQL (`gptbridge_xingcheng*`) remains
> the formal structured metadata authority; xstore is scoped to
> objects/snapshots/content-hashes/derived indexes only. The §46-§52
> shadow→parity→flip batch is void.
>
> Successor phase in force: **Capability Maturation Closure directive
> §0-§129** (2026-10-02) — no new capabilities/taxonomies/ladders; the
> goal is pushing every canonical capability through
> IMPLEMENTED→EVALUATED→CERTIFIED→MATURE behind floors, baselines and
> protected regression. Phase-1/2 landings:
> `CapabilityMaturityService.cs` (`star-capability-maturity/v1` state
> store at `runtime/state/capability-maturity.json` — §5 state machine
> with an evidence-derived ceiling; §12 floor axes incl. runtime/
> architecture/stability; §16/§17 baseline refresh on certify/mature
> only; §14 MATURE ⇒ PROTECTED; §10 auto-REGRESSED; §11 REOPENED keeps
> history) and `CapabilityRegressionMatrix.cs`
> (`star-capability-regression-matrix/v1` — §24 rows=candidate
> capability × protected columns, cells derived from the graph + §26
> core set; §28 runtime axes scored separately, §29). Verbs:
> `--capability-maturity` (§100-§101 report), `--capability-floor`,
> `--capability-transition`, `--capability-matrix`.

Capabilities are **first-class descriptors**, never their own runtime /
model / store / scheduler. All capabilities ride the single xc-fused-1
HybridCausalDecoder core, the single NativeTrainer, the single
NativeInferenceEngine and the single Lifecycle plane.

Phase-1 implementation (C# governance lane,
`xingcheng/src/backend/csharp/GPTBridge.XingchengLearning`):

- `CapabilityDescriptor.cs` — `star-capability-descriptor/v1`; §4 field
  set + §31 architecture binding + §66 floor + §83 aliases; closed
  vocabularies for class (`MODEL_NATIVE`/`RUNTIME_AUGMENTED`/
  `SERVICE_AUGMENTED`), owner plane (`GOVERNANCE`/`COMPUTE`/`DATA`/
  `SYSTEM`/`MIXED`), status (`UNAVAILABLE`/`IMPLEMENTED`/`TRAINING`/
  `EVALUATED`/`CERTIFIED`/`MATURE`/`REGRESSED`), resource hints.
- `CapabilitySeed.cs` — canonical capability rows (code = source of
  truth; persisted file is the governed projection).
- `CapabilityRegistry.cs` — `star-capability-registry/v1` at
  `xingcheng/runtime/state/capability-registry.json`; `Resolve`
  (alias→canonical, fail-closed), `Emit`, `Validate`.
- `CapabilityGraph.cs` — `star-capability-graph/v1` at
  `xingcheng/runtime/state/capability-graph.json`; §12 edge vocabulary
  (`REQUIRES`/`SUPPORTS`/`REGRESSES_WITH`/`SHARES_DATA_WITH`/
  `SHARES_EXPERT_WITH`/`EVALUATED_BY`); `RegressionSuiteFor` derives a
  lane's regression set automatically (§45: self + REQUIRES closure +
  REGRESSES_WITH neighbourhood + every protected/frozen capability).
- `CapabilityEvaluationMap.cs` — `star-capability-eval-map/v1`;
  existing eval categories keep their names (§85) and map to canonical
  ids (one capability ↔ many evals, §18/§19).
- `CapabilityProgressionPolicy.cs` — `star-capability-progression/v1`
  facade over `Maturation300M`: registry answers "which capabilities
  exist?", progression answers "which may train now?" (§16).
- `CapabilityConsistency.cs` — `star-capability-consistency/v1`, the
  §102 check battery (unknown/duplicate capability, orphan eval,
  orphan training path, missing binding, missing regression dep,
  missing evidence, capability/runtime confusion) — the release-gate
  consistency gate substrate (§101).
- `CapabilityEvidence.cs` — `star-capability-evidence/v1` (§20):
  append-only hash-bound evidence chain at
  `xingcheng/runtime/state/capability-evidence.jsonl`; records carry
  capability_id/model_version/candidate_id/dataset_snapshot/
  eval_suite/baseline/result/regression/runtime_profile/
  resource_profile/contribution/timestamp/evidence_hash;
  RUNTIME_AUGMENTED rows must attribute model vs runtime
  contribution (§7).
- `CapabilityDelta.cs` — `star-capability-delta/v1` (§90-§92):
  registry-vs-registry promotion delta reporting improved / unchanged
  / regressed / unsupported / newly_certified; a protected capability
  below floor or any certified-capability regression blocks
  promotion.

Verbs (`xc-learning.exe`): `--capability-registry` (emit+persist),
`--capability-graph`, `--capability-eval-map`,
`--capability-progression`, `--capability-resolve --capability <name>`,
`--capability-validate [--file <f>]`, `--capability-consistency`,
`--capability-regression-suite --capability <id>`,
`--capability-evidence --file <f.json>` (record §20 evidence),
`--capability-evidence-status [--capability <id>]`,
`--capability-delta --baseline <f> --candidate <f>` (§90-§92
promotion delta; protected-capability regression blocks promotion),
`--capability-runtime-profile --capability <id>` (§55-§57: one
CompiledExecutionPlan profile per capability — BALANCED /
CONTEXT_HEAVY / REASONING_ENABLED / TOOL_STRICT / EDGE; never a new
runtime, never kernel selection), `--failure-attribute --file <f>`
(§46-§48: every failure classifies to MODEL / RUNTIME / DATA / TOOL /
RESOURCE / SERVICE / MIXED before any training lane),
`--arch-limitation-record --file <f>` /
`--arch-limitation-status [--capability <id>]` (§52-§53 plateau
evidence; §54 precondition of `--arch-gate`).

Phase-4 wiring (§46-§54, §100): `--failure-record` runs
`CapabilityFailureAttribution` first — a RESOURCE_FAILURE is routed
to the governor and never enters the failure pool; the verdict echo
carries `attribution` + `pool_eligible`/`trainable`. `ArchitectureGate`
(`--arch-gate`) now requires a recorded
`star-architecture-limitation-evidence/v1` entry when a justification
claims `existing_architecture_cannot_solve` for a named capability —
the claim alone demotes to unmet. Acceleration-plane verbs
(`--training-pilot`, `--training-batch-plan`, `--speed-gate`)
accept an optional `capability` field — resolved through the registry
(fail-closed) and stamped on the plan as `capability_id` +
`runtime_profile` + `resource_hint` (§63/§100; plans still never
select kernels or demand resources).

Admission wiring (phase 2, §82/§98): SFT jobs and the
single-capability recovery lane validate declared capabilities through
the registry (`CAPABILITY_UNKNOWN` fails closed) before the maturation
sequence guard applies; canonical ids translate back to sequence
spellings (`reading` → `reading_grounding`).

`FeatureCatalog` is the *Implementation Feature Catalog* (§29): it
answers "which mechanisms exist?", never "which capabilities are
mature?". Phases 1-5 landed: registry/graph/eval-map/progression/
consistency (phase 1), admission wiring (phase 2), evidence + delta
(phase 3), failure attribution + runtime profiles + architecture
limitation evidence (phase 4), release-gate consistency wiring
(§101). Later (pending): alias convergence of legacy names (§103).

## 星澄 Model Maturity (`star-model-maturity/v1`)

> Normative authority: Codex B134/B135。
> Retired lane (B166/B167/B38): the `python.exe -m ...maturity`
> invocations and `native_transformer/maturity.py` implementation below
> are removed; they remain as interface documentation only until the
> governed owner-language entries land. Do not run them.

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
& main-system\.venv\Scripts\python.exe -m xingcheng.infrastructure.native_transformer.maturity --checkpoint <final.pt> --tool-root "xingcheng" --device cpu --save

# architecture-only ladder (L0-L2; L3+ reports skipped)
& main-system\.venv\Scripts\python.exe -m xingcheng.infrastructure.native_transformer.maturity --preset small

# latest certified level
& main-system\.venv\Scripts\python.exe -m xingcheng.infrastructure.native_transformer.maturity --status --tool-root "xingcheng"
```

Reports: `xingcheng/xingcheng/runtime/logs/maturity-*.json`; latest state:
`xingcheng/xingcheng/runtime/state/model-maturity.json`. Implementation:
`native_transformer/maturity.py` (`certify`, `current_maturity`,
`persist_report`).

## 星澄 Data Retention (`star-retention-policy/v1`)

> Normative authority: Codex C17/C18。
> Native lane landed (B167/B38 successor): `Retention.cs` inside
> `GPTBridge.XingchengLearning` (`xc-learning.exe`, C#) — same policy,
> same fail-closed boundary rules as the retired Python lane.
> Tunables single source: `xingcheng/xingcheng/runtime/settings/retention.json`。

Bounds Xingcheng runtime growth: old governed job dirs, logs, maturity /
self-learning reports and SFT snapshots are pruned by count and age.
**Never deletes** paths referenced by any `lifecycle.json` artifact version
or the checkpoint pinned in `xingcheng/xingcheng/runtime/settings/native-engine.json`
(unresolvable paths are fail-closed kept). Deletions append to
`xingcheng/xingcheng/runtime/logs/retention.jsonl`. Policy:
`xingcheng/xingcheng/runtime/settings/retention.json` (`enabled=false` disables everything).
Scheduled operation (Xingcheng-internal cut): no external scheduler
exists by design — the retired `SelfLearningDriver` registration path
is gone with the Python lane and no system-channel dispatch will be
built. Retention executes only inside the tool: at the end of every
self-learning cycle, or manually via `xc-learning.exe --retention
[--apply]`. When self-learning is disabled, coverage drops to manual
triggers — top up with `--retention` as needed. Kill switches: manifest `enabled=false` stops the
schedule; `retention.json` `enabled=false` stops deletion. Manual:

```powershell
# dry-run (default) / apply / status
& "xingcheng\src\backend\csharp\GPTBridge.XingchengLearning\publish\xc-learning.exe" --tool-root "xingcheng" --retention
& "xingcheng\src\backend\csharp\GPTBridge.XingchengLearning\publish\xc-learning.exe" --tool-root "xingcheng" --retention --apply
& "xingcheng\src\backend\csharp\GPTBridge.XingchengLearning\publish\xc-learning.exe" --tool-root "xingcheng" --retention --status
```

Implementation: `GPTBridge.XingchengLearning/Retention.cs`
(`apply_retention` port; `star-retention-policy/v1`).

## 星澄 Generation Migration (`star-generation-migration/v1`)

> Human-governor directive 2026-09-30: 世代升級採單一活躍世代——
> 新世代完成遷移、驗證、認證及啟用後才允許淘汰前代；前代有價值
> 資料（人格/記憶/知識/RAG/訓練/評估/工具/tokenizer）必須已
> move-forward 至新世代，歷史版本號累積不歸零。

Single-active-generation upgrade flow in `xc-learning.exe`:

1. `--gen-begin` creates the sole CANDIDATE manifest
   (`xingcheng/xingcheng/runtime/state/generation/migration-*.json`) recording
   source/target generation, checkpoint+tokenizer hashes, schema
   range and `weight_migration_method` (`direct` / `partial` /
   `distill`; `partial` requires `--expert-lineage <json>` —
   source→target expert weight source / init / router mapping /
   split-merge). A second open candidate is refused
   (`GEN_CANDIDATE_EXISTS`).
2. `--gen-record` moves each required domain forward with record
   counts — `personality`, `cognition_knowledge`,
   `long_term_memory`, `rag`, `training_corpus`,
   `evaluation_history`, `capability_state`, `tokenizer`,
   `routing_policy`, `safety_policy` — each must reach `migrated`
   or `not_applicable` (never parallel database copies).
3. `--gen-certify` runs fail-closed gates: candidate artifact hash,
   single candidate, tokenizer loadable, config contract,
   `xingcheng_trainer --smoke`, `xc_modeltool cache-smoke` native
   inference on the target bundle, optional `--suite` capability
   regression vs the source bundle (via `Evaluation`), all data
   domains complete, expert lineage present for `partial`.
   Any gate failure → manifest `FAILED`, nothing activated.
4. `--gen-promote` (requires certified): registers+activates the
   target weights in the model lifecycle, pins
   `xingcheng/xingcheng/runtime/settings/native-engine.json`, flips
   `state/generation/state.json` ACTIVE_GENERATION.
5. `--gen-purge` (dry-run unless `--apply`): deletes predecessor
   executable artifacts — unreferenced bundles and retired weight
   versions — never the target, the pinned checkpoint, or any
   lifecycle-owned active path. The manifest migrates forward with
   the new generation, preserving lineage after predecessor
   deletion.

```powershell
$X = "xingcheng\src\backend\csharp\GPTBridge.XingchengLearning\publish\xc-learning.exe"
& $X --tool-root "xingcheng" --gen-begin --target v28 --weights <bundle|ckpt> --weight-method direct
& $X --tool-root "xingcheng" --gen-record --manifest <id> --domain personality --status migrated --migrated 8
& $X --tool-root "xingcheng" --gen-certify --manifest <id> [--suite <suite.json>]
& $X --tool-root "xingcheng" --gen-promote --manifest <id>
& $X --tool-root "xingcheng" --gen-purge --manifest <id> [--apply]
& $X --tool-root "xingcheng" --gen-status [--manifest <id>]
```

Implementation: `GPTBridge.XingchengLearning/GenerationMigration.cs`;
directory artifacts (native bundles) hash via manifest/weights digest
in `Lifecycle.cs::ArtifactHashDir`. `--gen-promote` runs §18
post-activation verification (pin → active artifact, lifecycle active
weights, independent native inference) before reporting PROMOTED — a
failure rolls back pin + lifecycle active version + generation state;
`--gen-purge --apply` stamps a `lineage` block (predecessor identity,
hashes, activation/retirement times, carried-record counts) that
survives the deleted runtime.

## 星澄 Capability Trace (`star-capability-trace/v1` / `star-capability-result/v1`)

> Human-governor directive 2026-10-01: capability training is unfrozen.
> `capability_training_frozen=false` and
> `capability_training_mode=ALL_CAPABILITIES`; capability traces may feed
> governed training only through the existing quality, permission,
> resource, evaluation, rollback and activation gates.

- Level 2 request trace (`xingcheng/xingcheng/runtime/logs/capability-trace.jsonl`):
  `request_id`, `intent`, `service_expert`, `model_generation`,
  `architecture_generation`, `router_layers[]`
  (`layer_id`/`router_type`/`selected_neural_experts`/`shared_expert_used`),
  `tool_used`, `rag_used`, `final_result`, `capability_eval`.
- Level 1 expert result
  (`xingcheng/xingcheng/runtime/logs/capability-results.jsonl`):
  `capability`, `status` (pass/fail/degraded/skipped), `evidence`,
  `confidence`, `source`, `failure`, `fallback`, `trace_id`.

```powershell
& $X --tool-root "xingcheng" --trace-record --trace <file.json>
& $X --tool-root "xingcheng" --cap-record --result <file.json>
& $X --tool-root "xingcheng" --trace-status
```

Implementation: `GPTBridge.XingchengLearning/CapabilityTrace.cs`
(append-only JSONL; generation identity auto-stamps from the active
generation state).

## 星澄 Model Core Axis (`star-model-core/v1` / `star-architecture-taxonomy/v1`)

星澄只有**一個**模型核心：**HybridCausalDecoder** —— 單向因果、自回歸、
decoder-only，內部交錯 DeltaNet recurrent layers（`DELTA_RECURRENT`）與
週期性 Full Attention layers（`FULL_ATTENTION`，canonical `xc-fused-1`
為 interval=4 → `D D D A` 重複）。**不得**再使用「多核心軸」「多模型核心」
的表述；DeltaNet 與 Full Attention 是同一核心的兩種 layer type，不是兩顆核心。

其他全部是正交軸，每個 feature 恰有一個 `primary_axis`：

- `MODEL_COMPONENT`（GQA/QKNorm/AttentionGate/RMSNorm/SwiGLU/Embedding/LMHead）
- `EXPERT_AXIS`（MoE/Router/SharedExpert — 模型側）
- `POSITION_AXIS`（RoPE/PartialRoPE/YaRN）
- `MODALITY_AXIS`（Vision early fusion 餵同一核心；audio/video contract-only）
- `TRAINING_AXIS`（MTP 是 training auxiliary，不是 inference core）
- `STATE_AXIS`（KV/PagedKV/KV-INT8/PrefixCache/DeltaState…，唯一 owner 為
  state manager；PrefixCache = STATE_AXIS + runtime_optimization tag）
- `RUNTIME_OPTIMIZATION_AXIS`（ExpertOffloading/Prefill-Decode/CUDA/
  Speculative… — 永不改變模型語意，也不產生新 architecture generation）
- `PRECISION_AXIS`（FP64/BF16/INT8… — runtime/storage policy，非世代身份）
- `CAPABILITY_AXIS`（RAG/Thinking/Persona/Roleplay/Agent… — 能做什麼，
  不是架構）
- `GOVERNANCE_AXIS`（XCN10/Lifecycle/Migration/Audit… — 不進 forward path）
- `EXPERIMENTAL_ARCHITECTURE`（MLA/CSA/Gemma4/KDA/Mamba/RWKV/AttnRes/
  LatentMoE/MSA — 永不列入 canonical core）

Version 維度分開：`architecture_generation` / `weight_version` /
`runtime_version` / `state_contract_version` / `bundle_version` /
`capability_version` / `evaluation_version` — 不得再用單一編號混表。
`architecture_contract_hash`（star-model-core/v1 canonical JSON 的 sha256）
在 job/checkpoint/bundle/runtime 必須一致，否則
`ARCHITECTURE_CONTRACT_DRIFT` fail-closed。

```powershell
& $X --tool-root "xingcheng" --taxonomy          # 軸表
& $X --tool-root "xingcheng" --core-contract    # star-model-core/v1
& $X --tool-root "xingcheng" --axis-checks      # §42 電池
& $X --tool-root "xingcheng" --version-dimensions
```

Implementation: `GPTBridge.XingchengLearning/ArchitectureTaxonomy.cs`、
`AxisChecks.cs`；feature registry 的 `primary_axis` 由
`FeatureCatalog.FeatureDict` 經 taxonomy `Classify` 派生。

## 星澄 Language Architecture（xingcheng enclave 收斂目標）

> Normative authority: Codex B81 `LANGUAGE-OWNERSHIP`（rev 196，語言-
> 職責指派入專法）。實作層（檔案、API、kernel 劃分）仍為 owner-local
> （B81 `ARCHITECTURE-EXCLUSION`）。此表為專法條文的操作手冊投影；
> 在 `xingcheng` enclave 樹內覆寫上方 Execution Plane
> Ownership 的 repo 全域預設。

| 語言 | 角色 | 比重 | 判斷 |
| --- | --- | --- | --- |
| C++23 | 模型核心、Tensor、Forward/Backward、MoE、Attention、Delta、MTP、CPU Kernel | 最大 | 主計算語言 |
| Rust | 儲存、資料、檔案格式、Tokenizer、驗證器、並行 IO、安全邊界 | 第二 | 主系統語言 |
| C# | Governance、Lifecycle、自治訓練、Scheduler、Policy | 第三 | 主控制語言 |
| C | 穩定 ABI、極低階 SIMD/OS bridge | 極少 | 只做邊界 |
| F# | 無預設 Production 職責 | 0 或極少 | 不建議強制使用 |

現況與收斂差距（2026-10-01 盤點）：

- **C++23 已就定位** — `native_transformer/xct_*`（訓練核心）、
  `xcm_*` + `xc_modeltool`（模型工具）、`backend/cpp/engine_*` +
  `cuda_*`（推論 + PTX kernel）。
- **C# 已就定位** — `GPTBridge.XingchengLearning`（SelfLearning/
  JobExecutor/Evaluation/Lifecycle/Retention）+ `xct-executor`。
- **Rust lane 尚未建立** — 其職責目前在 C++：`engine_tokenizer.h`
  （tokenizer）、`xct_ckpt.h`/`engine_weights.h`/`xcm_corpus.h`
  （檔案格式與資料）、驗證器散在 `xcm_*cert`/`xcm_rtgates`。
- **F# 待退役** — `GPTBridge.XingchengEval`（xc-eval）目前是
  `verdict_owner: "fsharp"` 的評估仲裁 lane；收斂時判決邏輯遷入
  C#，`verdict_owner`/parity 檢查同步更新。在此之前 F# lane 繼續
  持有現行契約，不得提前拔除。
- **C 已就定位** — `native/bridge/gptbridge_native.c` +
  `engine_c_abi.cpp`/`xingcheng_engine_c.h` 維持 ABI 邊界，不長肉。

### 星澄 Native Contract（XNC，Codex B81 `NATIVE-CONTRACT` / rev 200）

XNC 是星澄域內**唯一** artifact contract 家族：控制面 = versioned
binary/text manifest；資料面 = packed binary。註冊成員：`XCN`
（model checkpoint）、`XDS`（dataset）、`XCR`（receipt）、`XST`
（state）、`XEV`（evaluation）。C++23 / Rust / C# 各 lane 實作自己的
reader/writer，共守同一份 owner-local byte-level spec——法典只綁
家族唯一性、雙平面切分、成員種類與跨語言一致義務，byte layout
不寫入法典。成員識別以名稱經權威 contract registry 解析（rev 199
version-neutrality），法典內不釘版本後綴。

Owner-local byte-level spec：`xingcheng/contracts/xnc-spec.md`
（`xnc-spec/v1`——magic/端序/envelope/成員 registry/XCN1+XCB1 逐位元版面/
manifest 規則/三語言 conformance）。Canonical 測試向量：
`xingcheng/contracts/xnc/vectors/`。

## 星澄 Fast/Slow Capability Plane（Laya + MiMo-V2.6 原生吸收）

同一 **HybridCausalDecoder** 提供兩條能力路徑 —— System-1 不是第二顆
模型，是用同一 weights / tokenizer / prefix cache 的**決策層**：

- **SYSTEM_1 fast path**：prefill → `NativeSystemOneHead` →
  `star-typed-decision/v1`（BOOLEAN / CHOICE / ORDINAL_SCORE /
  CONFIDENCE）→ DONE，**永不進 autoregressive decode**
  （`decode_tokens = 0`）。第一批只服務 RAG_REQUIRED / TOOL_REQUIRED /
  TOOL_CLASS / CONTINUE_STOP 等已驗證域；未驗證域 fail-closed
  `SYSTEM1_DOMAIN_UNCERTIFIED`。
- **Calibration**：softmax 機率不等於信心 —
  `DecisionCalibrationLayer`（temperature + per-option-count），
  `star-decision-calibration/v1` 報 ECE/Brier/NLL/histogram；
  `calibrated_confidence` 低於門檻 → `ABSTAIN` → fallback SYSTEM_2。
- **Head artifact**：`decision-head.bin`（`star-system1-head/v1`）
  綁定 model_hash + generation + hidden_size；不相容 → fallback，
  主模型永遠能啟動。本階段**不**升 XCN11。
- **Decision trace**：每次 fast decision 寫
  `xingcheng/xingcheng/runtime/logs/decision-trace.jsonl`（probabilities /
  confidence / latency / model hash / generation）。
- **MiMo router stability**：`RouterStabilityPolicy` —
  PRETRAIN=TRAINABLE、SFT/BASELINE_RECOVERY=GOVERNED、
  LARGE_AGENT_RL=**FROZEN_BY_DEFAULT**（RL 不許漂移 routing
  distribution）；`RouterStabilityGate` 監 entropy/utilization/
  drift，超限 `ROUTER_DRIFT_EXCEEDED`。
- **Agent learning（schema-only，RL 未解凍）**：
  `star-agent-trajectory/v1` 是唯一 trajectory schema；
  `HarnessRegistry` 多 harness + seen/unseen → `HARNESS_OVERFIT`；
  `GroupwiseTrajectoryEvaluator` 先排 incorrect 再比
  cost/path；`RewardIntegrityGate` grader→verifier→consistency→
  adversarial，單一 grader 永不直接定 reward
  （`REWARD_VERIFIER_MISMATCH` / `REWARD_SUSPECT`）。
- **MTP**：`NativeMtpDrafter` = RUNTIME_OPTIMIZATION +
  TRAINING auxiliary，非第二核心；drafter 可用更激進 precision
  （main verify 保證語意）；speedup ≤ 0 自動關閉。
- **RL 解凍順序**（§33）：100M capability parity → System-1
  supervised calibration → trajectory collection → self-correction
  SFT → DPO → bounded GRPO。目前只到 schema/evaluator。

```powershell
& $X --tool-root "xingcheng" --system1-checks     # §44 電池
& $X --tool-root "xingcheng" --typed-decision-validate --file <f.json>
& $X --tool-root "xingcheng" --cognition-route --file <f.json>
& $X --tool-root "xingcheng" --router-stability --file <f.json>
& $X --tool-root "xingcheng" --trajectory-validate --file <f.json>
& $X --tool-root "xingcheng" --reward-gate --file <f.json>
```

Implementation: `GPTBridge.XingchengLearning/SystemOne.cs`
（typed decision / calibration / abstention / cognition router /
trace / head binding）、`AgentLearning.cs`（trajectory / harness /
groupwise / reward integrity / self-correction）、
`RouterStability.cs`（stage policy + drift gate）、
`LayaMiMoChecks.cs`（20-check §44 battery）。

## 星澄 NativeMemoryCudaPlane（memory/CUDA directive）

**唯一** CUDA 記憶體平面 —— 所有 device/pinned 配置走
`UnifiedCudaMemoryManager`；hot path（decode / layer forward /
MoE dispatch / KV append / Delta update / MTP verify / training
microstep）**永不** cudaMalloc/cudaFree。

- **Tiers**：`PINNED_PERMANENT`（common weights/router/shared
  expert）、`SESSION_PERSISTENT`（KV/Delta state/hot experts）、
  `TOKEN_PERSISTENT`、`LAYER_TEMP`、`KERNEL_SCRATCH` ——
  不重疊生命週期 alias 同一物理記憶體。
- **CudaDevicePool**：`cudaMallocAsync` mempool，
  release threshold = high-water —— 只有 memory pressure /
  unload / generation switch / 明確維護才 trim。
- **Budget**：VRAM hard budget 永留 emergency headroom；
  記憶體不足走 8 步 **pressure ladder**（cold prefix → warm
  prefix → routed expert → hotset → batch → prefill chunk →
  spill → reject），**不得 OOM**。
- **PinnedHostPool**：固定 ring buffer，上限
  `max_pinned_host_bytes`；普通 metadata/corpus 用 pageable。
- **Streams**：固定 lane（DECODE_HIGH / PREFILL / EXPERT_PREFETCH /
  H2D / D2H / TRAIN），decode 最高優先權；日常同步用 event，
  不用 cudaDeviceSynchronize。
- **Precision（sm_86）**：production = BF16（Tensor Core），
  router/norm accumulate = FP32，KV = INT8，
  **FP64 = Oracle only**（gradcheck/parity/certification），
  **FP8/FP4 = DISABLED_BY_HARDWARE**。
- **Telemetry**：`star-cuda-memory-telemetry/v1`（pool
  used/peak、workspace_peak、per-tier bytes、h2d/d2h/d2d
  bytes、pinned、ladder events）。
- **整個 plane 屬 RUNTIME_OPTIMIZATION_AXIS** —— 不改模型語意、
  權重語意、XCN10、HybridCausalDecoder，不產生新 generation。

```powershell
# native probe（真 GPU 執行 pool/arena/ladder 檢查；無 GPU 回報 simulated）
& xc_modeltool.exe memplane-probe --budget 2147483648 --pinned 33554432
& xc_modeltool.exe memplane-telemetry
# 合約層電池（原生 probe + 政策檢查）
& xc-learning.exe --tool-root "xingcheng" --cuda-plane-checks
& xc-learning.exe --tool-root "xingcheng" --precision-policy
```

Implementation：`tools/xcm_memplane.h`（manager/pool/arena/
pinned/ladder/telemetry，real CUDA runtime API）、
`GPTBridge.XingchengLearning/MemoryCudaPlane.cs`（precision
policy / ladder / prefill-chunk / telemetry schema / alignment）、
`CudaPlaneChecks.cs`（11-check battery）。

未落地（P1–P10，依優先序排程）：BF16 production GEMM 切換、
CUDA Graph decode/prefill/training、kernel fusion、fused AdamW、
activation/gradient arena、autotune —— 目前僅契約與 catalog
狀態；FP8/FP4 production kernel **不做**。

## 星澄 Data Residency (`xingcheng-internal`)

> Human-governor directive 2026-09-28: 星澄資料只能保留在星澄內部。

All xingcheng-owned data — weights, cpp-bundles, corpora, checkpoints,
lifecycle snapshots, self-learning pools/reports, ledgers, eval output,
and any recovery or scratch artifacts — resolves inside the registered
xingcheng domain roots only (`XINGCHENG_INSTITUTION_ROOT` =
`xingcheng/xingcheng/`, `STAR_DIRECTORY` =
`Standalone tools/model-dialogue/xingcheng/`; codex
`data_authority: residency XINGCHENG_DOMAIN_ONLY, no external
persistence`). Copies under `main-system/runtime/`, other tools, other
drives, or ad-hoc scratch dirs are violations and must be moved in or
deleted, never left behind.

Enforcement is fail-closed; the retired Python lane (B166) implemented
the boundary as follows — the policy still binds, and the native
successor must preserve it:

- `native_transformer/cpp_runtime.py::assert_inside_xingcheng` (retired)
  refused any path outside `tool_root()/xingcheng` with
  `XINGCHENG_DATA_BOUNDARY`. Applied to bundle export/staging targets,
  the execution ledger, and the pinned serving artifact at
  `generate_via_cpp_engine` (an out-of-boundary pin refuses to serve).
- `native_transformer/retention.py::apply_retention` (retired)
  re-checked every delete victim against the same boundary and skipped
  (counted as `boundary_skipped` in the audit entry) rather than
  touching a foreign path.

Operational test/fixture bundles and probe scripts live under
`xingcheng/xingcheng/runtime/devin/` so scratch work also stays in-boundary.

## 星澄 Training GPU Gate & Auto-Release

> Normative authority: Codex B44/B16。
> Retired lane (B166/B167/B38): the Python classes/modules referenced
> below (`TrainingJobExecutor`, `gpu_coordinator`, `auto_release.py`,
> `NativeTransformerEngine`, `chat_foundation_dataset.py`) are removed;
> the policy contracts remain binding on their native successors.
> Tunables single source: `xingcheng/xingcheng/runtime/settings/native-engine.json`＋bounded config keys（`gpu_required_mb`／`gpu_acquire_timeout_s`／`auto_release_idle_seconds`）。

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

> Normative authority: Codex B154/B155。
> Retired lane (B166/B167/B38): the Python modules referenced below
> (`core_system/app_lifecycle.py`, `boot_core_handover.py`,
> `test_p0_lazy_lifecycle_handover.py`) are removed; the lazy-start
> contract remains binding on the Rust/C# successors.

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

## 星澄 Governed Metasearch (searchd / `xingcheng-searchd/v1`)

Web search is served by **searchd**, a Go-native metasearch engine
(`Standalone tools/searchd-go/`, module `xingcheng/searchd`, go-service
layer per LanguagePolicy — versioned-contract consumer only). It replaces
SearXNG as the primary provider: concurrent fan-out to compiled-in
credential-free adapters (Wikipedia opensearch, Bing RSS, DuckDuckGo
Lite), URL-normalization dedupe, deterministic reciprocal-rank fusion
(k=60), bounded metadata-only results.

Governance boundary is unchanged: the only entry point is the governed
`xingcheng_web_search` command (former `local_ai_lifecycle._run_web_search`,
retired with the Python lane — B166),
which audits into `web_search_log` and returns bounded metadata.
Provider chain is driven by `xingcheng/xingcheng/runtime/settings/web-search.json`
(`provider`: `auto`/`searchd`/`searxng`; env `XINGCHENG_SEARCH_PROVIDER`
/`XINGCHENG_SEARCHD_URL`/`XINGCHENG_SEARXNG_URL` override; `auto_start`
lazily spawns `searchd-go/bin/searchd.exe`). `auto` = searchd first,
empty-or-error degrades to SearXNG; the repo's `web-search.json` pins
`provider: "searchd"` — SearXNG is retired from the default chain. searchd hard-fails to start on any
non-loopback listen address; outbound destinations are a compiled-in
allowlist (`*.wikipedia.org`, `*.duckduckgo.com`, `bing.com`) enforced at
the transport layer including redirects — it can never act as an
arbitrary proxy. Contract: `Standalone tools/searchd-go/CONTRACT.md`.
Portable Go toolchain lives in `.tools/` (gitignored).

```powershell
# build + test + run
cd 'Standalone tools\searchd-go'
go build -o bin\searchd.exe .\cmd\searchd
go test ./...
.\bin\searchd.exe   # 127.0.0.1:8091, POST /v1/search, GET /healthz
```

## On-Demand Model Activation (Lazy 星澄)

> Normative authority: Codex B154。
> Retired lane (B166/B167/B38): `main-system/src-core/tasks/model_service_activation.py`
> (`ModelServiceActivationBroker`) is removed; the activation contract
> below remains binding on its native successor.
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

- 實作（已退役，B166）：原 `xingcheng/application/codex_diagnostics.py` 與
  `governance_rule/execution/audit/architecture_docs.py`（診斷用）均已移除，
  待受管原生語言接替者落地。
- 路由：原 `tool_routes.py` 已退役；`(model-dialogue|star-chat) -> xingcheng`
  兩指令的唯讀註冊現存於 `tool_routes.json` port 檔。
- model-dialogue 於送出前若 owner 未啟動，會先走懶啟動；報告以 zh-TW 摘要顯示於對話。

## Resource Governor

> Normative authority: Codex B3/B16/B159。

Adaptive CPU/memory governor that watches every process owned by the current
user and lowers resource pressure automatically: sustained CPU hogs get
`BELOW_NORMAL` priority, extreme hogs get their CPU affinity capped to half of
the logical CPUs, and large idle processes have their working set trimmed
(`EmptyWorkingSet`).  Actions revert after ~5 calm minutes.  Per-process
dynamic升降 (`defaults`): with `limiter_dynamic` a capped worker's Job rate
steps tighter each cycle while it stays extreme (floor `limiter_min_percent`)
and relaxes back toward `limiter_percent` when demand drops below half the
cap; with `priority_escalate` a process still extreme past
`sustain + extreme_sustain` escalates `below_normal` → `idle` and steps back
when it drops under extreme (rule-held/pb/bg/foreground exempt).  Protected:
Windows system processes, security software (including the user's antivirus)
and the governor itself.

Per-backend-service dynamic control (`defaults`, all off unless declared):
with `pool_dynamic` each non-interactive pool's shared Job CPU envelope is
re-resolved every cycle — while the machine is pressured
(`cpu_load ≥ pool_relief_cpu_pct` or responsiveness strain / active
regulation) the envelope tightens by `pool_step_percent` toward
`pool_floor_percent`; when calm and the pool's demand rides its cap
(≥ 90% of the applied rate) it relaxes back toward the preset.  The
interactive lane is never squeezed, and the applied rate is reported as
`pools.<name>.cpu_applied_pct` in the snapshot.
RAM reclamation (`reclaim_enabled`): when machine `mem_used_pct` reaches
`reclaim_mem_pct` the governor trims the largest working sets ≥
`reclaim_min_mb` in batches of `reclaim_batch` per cycle (RSS-descending,
trim cooldown respected; foreground/excluded/governance exempt) so RAM is
auto-released before paging pressure builds.  GPU/RAM grant reclaim runs
through the grant lane: at `ACTIVE_PRESSURE`, `make_context` scales
`vram_budget_percent` by `grant_pressure_vram_scale` and the RAM share for
new grants by `grant_pressure_ram_scale`, so existing grants resize smaller
and cooperating services release VRAM/RAM on their next poll.

```powershell
# build
powershell -NoProfile -ExecutionPolicy Bypass -File native\resource_governor\build.ps1

# status / one-shot (dry-run first) / start / stop
& native\resource_governor\bin\resource-governor.exe --status
& native\resource_governor\bin\resource-governor.exe --once --dry-run
& native\resource_governor\bin\resource-governor.exe --start
& native\resource_governor\bin\resource-governor.exe --stop

# cross-reboot persistence (per-user Run key: no elevation needed)
& native\resource_governor\bin\resource-governor.exe --install-logon
& native\resource_governor\bin\resource-governor.exe --uninstall-logon
```

Tunables: `--interval` (default 20s), `--cpu-busy` (50% of one core),
`--cpu-extreme` (150%), `--mem-trim-mb` (1500), `--sustain` (3 samples),
`--no-affinity`.  Actions are logged to
`main-system/runtime/logs/resource-governor.jsonl`; the latest cycle snapshot
is in `main-system/runtime/state/resource-governor.json`.

**Automatic mode** (`auto_mode` + `auto` block in
`main-system/config/resource-governor-rules.json`): a demand-driven advisor
inside the same governor process (B159 — no second regulator) picks among the
registered `modes` presets each `auto.eval_interval_s` (60 s).  `auto.ceiling`
(default `medium`) is the highest mode auto-mode may select **while the user
is active**, so foreground / user work always keeps machine headroom; when the
user is idle ≥ `idle_after_s` (300 s, via `GetLastInputInfo`) the effective
ceiling relaxes to `idle_ceiling` (`high` — 閒置全速), and returning activity
urgently demotes anything above `ceiling` (streak/cooldown exempt).
`power_saving_schedule` (22:00–07:00) forces `sleep` at night.  Control law:
responsiveness strain or machine overload → `low` immediately (urgent,
cooldown-exempt); worker demand + machine headroom → upgrade after
`streak_up` evaluations, clamped to the effective ceiling; downgrades need
`streak_down` evaluations plus `cooldown_s`.  Signals are EMA-smoothed
(`signal_alpha`, default 0.5): machine overload and headroom judge the
smoothed value so a single busy/quiet sampling window cannot flip the mode,
while `strain_instant_margin` (default 10) keeps truly extreme spikes urgent;
`eval_interval_busy_s` (default 20, ≤0 disables) shortens the eval interval
to a busy cadence while strained, overloaded, or mid-transition so both
urgent response and calm recovery land sooner.
Manual mode selection via `app:set-resource-mode` sets `auto_mode=false`
(user intent wins).  Advisor state persists in
`main-system/runtime/state/resource-mode-advisor.json`; mode switches append
to `runtime/state/resource-mode-audit.jsonl` (same ledger the Rust backend
writes for manual changes).

**GPU/VRAM mode keys**: each `modes.<name>` preset declares `gpu_enabled`
and `vram_budget_percent` (`sleep`/`low` are CPU-only; `medium` 40%,
`high` 70%).  The Xingcheng training lane resolves the governor's current
`mode` from the state file, reads the same rules file, and admits the
trainer's opt-in CUDA lane (`XINGCHENG_TRAINER_CUDA_OPT` — resident w/m/v
fused AdamW) only when all of: the job requests `device: cuda|auto`, the
mode's `gpu_enabled` is true, `xc_modeltool probe-cuda` reports a device,
and free VRAM clears `train_cuda_min_free_mb` (default 2048, clamped to
the mode budget).  Denial is fail-closed to CPU lanes and recorded in the
job summary `cuda` block plus the resource-action ledger; `optimizer_lane`
in the summary reports `cuda-adamw` vs `cpu-native` (evidence =
admission+env-flag until the trainer report echoes the lane it ran).
`xc-learning --preflight` previews the whole gate read-only.

**Training concurrency = 1**: at most one governed training job is in
flight at a time — `RunJob` claims the lane in a single advisory-locked
transaction (queued check + sibling check + preflight transition +
audit), so concurrent executors can never both pass
(`EXECUTOR_TRAINING_SERIAL`, job stays queued). Orphaned live-state rows
(crash/kill never transitions out) are reaped explicitly:
`--reap-stale [--older-than-s N] [--apply]` (dry-run default, floor
3600 s, `EXECUTOR_ORPHANED_REAPED`).  Per-job CPU
lanes follow `classes.training.quota` in `concurrency-budget/v1`, tuned
through `concurrency_w_training`/`concurrency_min_training` in rules
`defaults` (base 5 → quota 5 at tier none, 2 at pre, paused at active;
training is last in fill order so it takes pool leftovers — raising it
further requires shrinking `interactive_share` or the model/rag
weights).  Job configuration `pack_tokens`/`pack_sep` enables
trainer-side token packing (fewer optimizer steps, larger GEMM M —
opt-in; lanes must size `max_steps`/`lr` accordingly).

Implementation: `native/resource_governor/` (C++23, Codex A137) — the Python
`scripts/resource-governor.py` and `resource_mode_advisor` lane were retired
on migration; the advisor control law now lives in
`governor_advisor.h/.cpp` (pure evaluation in-cycle) and the state/log JSON
contract is unchanged.

## Adaptive SQL Layer

> Retired lane (B166/B167/B38): the Python `shared_layer.adaptive`
> implementation and its tests are removed; the envelope contracts below
> remain binding on the governed native/C# successor. Only `.json` port
> files remain under `shared-layer/src/shared_layer/adaptive/`.

`shared-layer/src/shared_layer/adaptive/` is the bounded, pre-approved control
layer for the local data platform (admission control, dynamic pool/batch,
retry, per-domain breakers, maintenance scheduling, cost gate and Qdrant
budgets). PostgreSQL is the only SQL authority; no embedded SQL fallback is allowed.
Every adaptive parameter moves only inside
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

> Retired lane (B166/B167/B38): the Python `shared_layer.security`
> implementation and `test_security_control.py` are removed; the control
> contracts below remain binding on the governed native/C# successor.

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
  `assert_metadata_only`; stored credential verifiers use HMAC-SHA256.
- Rotation with grace period (create → verify → switch → grace → revoke) and
  strict emergency revocation (disable → terminate → rotate → raise
  generation → audit).
- Security generation fence: sensitive writes fail closed under a stale
  `gptbridge.security_generation`; raise via
  `gptbridge_security.raise_security_generation(reason, actor)`.
- Mandatory Qdrant scoping (`require_scope`, module_id always required);
  retired embedded SQL storage must have zero active consumers.
- Least-privilege certification: `least_privilege_report()` +
  `certification_errors()` (no SUPERUSER/CREATEDB/CREATEROLE/BYPASSRLS,
  no PUBLIC grants).
  Tests: `shared-layer/tests/test_security_control.py`.

## Cross-Engine Workflow (Saga)

> Retired lane (B166/B167/B38): the Python `shared_layer.workflow`
> implementation and `test_workflow_consistency.py` are removed; the
> Saga contracts below remain binding on the governed native/C#
> successor.

`shared-layer/src/shared_layer/workflow/` makes one business operation across
PostgreSQL + Qdrant + NTFS recoverable, re-runnable and verifiable —
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
  never exactly-once claims). Central transport and durable operation state
  stay in PostgreSQL; no local SQL store may declare completion.
- Publish barrier: `PREPARING → INDEXING → VERIFYING → READY`; only `READY`
  is readable, and cross-engine verdicts degrade to `DEGRADED` / `CONFLICT`
  instead of pretending success.
- Atomic file writes (`temp → fsync → hash → rename`), tombstone deletes,
  operation identity digests (native/PostgreSQL parity), and a guard that forbids
  cross-engine work inside an open PostgreSQL transaction.
  Tests: `shared-layer/tests/test_workflow_consistency.py`.

## Architecture Registry (single source of truth)

> Normative authority: Codex C31/C35/C45。

`governance_rule/execution/audit/architecture_registry.json` is the one
machine-readable topology authority: every component declares
`component_id / architectural_role / runtime_form / owner_sovereign /
owner_sub_sovereign / execution_identity / physical_path / lifecycle /
canonical / dependencies / information_channels`.

- `architectural_role` (governance / startup / decision / information / data /
  execution / model / development-maintenance …) is deliberately separate
  from `runtime_form` (native-process / desktop-shell / standalone-service /
  database / external-service …), so "directory", "tool", "module",
  "service" and "layer" can no longer conflict.
- Current ownership follows only the five-core responsibility model recorded
  by the Codex; historical ownership layers are never active owners.
- The governance audit runs `check_architecture_registry`: Codex == registry
  == permission-directory routes == module manifests == sovereign ownership
  == physical directories, in one pass.  Drift is a failure, not a warning.
- Do not create a second copy of the topology: docs, manifests and code must
  reference this registry instead of restating it.

Tests: the Python `test_architecture_registry.py` lane is retired (B166);
registry drift is covered by the native audit engine checks.

## Work authority

Do not create separate planning authorities. Work is governed directly by the
PostgreSQL Codex, registered contracts, and explicit user instructions.

## Codex update pipeline

> Normative authority: Codex D75/D118/B124/C102.

PostgreSQL is the sole Codex authority. A change uses an isolated, non-authority
candidate artifact, rebuilds current bindings and all projections, synchronizes
the five-part Chinese mirror and architecture documents, runs the governance
checks, and atomically publishes one newer version. The published
`current_version`, active binding version, revision history and seal generation
must agree. Temporary candidate formats never acquire authority.

Unavailable prerequisites are `DEFERRED`, not PASS or rejection. Deferred work
records its reason and next evaluation time and resumes through the existing
automation flow. Package candidates without explicit permission use
`DEFERRED_AWAITING_PERMISSION`; normal releases younger than 14 days use
`DEFERRED_OBSERVATION_WINDOW`; the update detector runs every 24 hours.

## Governance

- PostgreSQL is the official Codex; generated mirrors and architecture documents
  are synchronized projections and must not diverge from it.
- Governance audit must pass before commits; the governed path is the native
  engine `native/test_suites/bin/audit-engine.exe --manifest
  governance_rule/execution/audit/audit_checks_manifest.json --root E:\GPTBridge`
  (the pre-commit hook runs it automatically via `GPTBridge.GitAutomation.exe`,
  including the governed C# manifest refresh lane).
- **Implementation precedence**: preserve a verified superior implementation
  and converge the Codex or registered contract; never roll back superior
  behavior to match retired text.
- Model core must remain separate from network functionality.
- All external network access must go through governed tool paths.

## Verification Commands

```powershell
# Native test suite fleet (MSVC build + bounded parallel run;
# emits native/test_suites/bin/native-report.json). pytest is retired —
# Python tests must never be added or executed (codex: PYTHON:none).
powershell -ExecutionPolicy Bypass -File native/test_suites/build.ps1

# Governance audit (native engine; the Python audit lane is retired)
& native\test_suites\bin\audit-engine.exe --manifest governance_rule\execution\audit\audit_checks_manifest.json --root E:\GPTBridge

# Refresh the audit manifest when it is stale (governed C# exporter lane)
& shared-layer\csharp\GPTBridge.GitAutomation\publish\GPTBridge.GitAutomation.exe --manifest-export --root E:\GPTBridge
```

## Build Commands

- Native production runtime: C/C++23, Rust, C#, F#, Go and Julia according to
  the language ownership contract.
- UI: Rust + Tauri + Native JavaScript ESM with JSDoc + GPUI + egui; Esbuild
  and SWC may be combined as governed build tools.
- Python: fully retired (B166/B167/B38). No Python source, interpreter,
  virtual environment, package manager, dependency, build/test/audit/
  training/inference or fallback path exists or may be added anywhere.

## Native UI stack

The only current UI architecture is Rust 1.98.1 application/state/security,
IPC, lifecycle and native integration; Tauri desktop shell and WebView host;
Native JavaScript ESM with JSDoc for general UI; GPUI for model dialogue,
coding and high-volume native views; and egui for diagnostics, profiling and
governance inspection. Retired UI stacks must not be restored as runtime
dependencies or fallback hosts.

## Bounded Parallelism

Parallelize only independent work units with explicit bounds: RAG candidate
search branches, embedding batches, file scans, network requests, independent
SQL reads, independent audit checks, model preprocessing, tool health probes,
and independent build/test units. Keep dependent stages ordered at the merge
boundary (for example dense/sparse/code retrieval → fuse → rerank), and do not
split tiny functions, frequent cross-language calls, small JSON conversions,
shared-cache-line state, or lock-heavy work. Retrieval ownership remains the
Rust `vectord-rs` path; parallel retrieval must remain bounded and governed.

## Cross-Language Round-Trip Budget

Minimize cross-language round-trips on hot paths. Prefer one contract boundary
from the application to the native owner rather than chains such as Rust → C#
→ Python → Rust → C++. The canonical flows are:

- inference: Rust model service → C++ inference → Rust;
- RAG: application → Rust RAG / `vectord-rs` → result;
- training: Rust/C# scheduler → native C++ training engine → artifact →
  process exit (the Python + JAX lane is retired, B166).

Python may not be installed or invoked for any purpose — governed training and
verification run on the registered native owner languages only (B166).

## Model Runtime Residency

Model runtime residency is resource-selective, not service-selective. While a
model is hot, reuse one C++ inference process with the mapped weights,
reusable tokenizer, bounded KV-cache pool, allocator arena, scratch buffers,
and bounded request queue. Do not reload weights, allocate large buffers, or
reinitialize/destroy the runtime for every request. When the model has no active
use, the existing `AutoRelease` policy must reclaim cold resources. The target
is hot-resource reuse with cold-resource recovery, not unconditional residency
and not unconditional reconstruction.

## Event-Driven UI State

UI surfaces are fully event-driven across Backend → Rust State Core → GPUI,
JavaScript-ESM, or egui. Publish changed state only; do not poll the backend
from the UI on a fixed interval such as 100 ms. Streaming model output must be
coalesced into small bounded updates before GPUI redraws; batch size and time
window are benchmark parameters, not assumed constants such as 16, 32, or 64.
GPUI is the formal core view for model dialogue, streaming text, and virtual
lists; Rust/Tauri and JavaScript-ESM integrate through governed state events.

## Hot-Path Allocation Policy

Reduce allocation churn on profiled hot paths: avoid repeated `malloc/free`,
`new/delete`, unreserved `Vec` growth, `String` reallocation, temporary DTOs,
and temporary JSON. Prefer capacity reservation, bounded buffer pools, object
reuse, arena/scratch allocation, stack values, small fixed structs, and
span/slice/view interfaces. Prioritize RAG queries, token decode, IPC frames,
audit records, and network buffers. Do not build a project-wide custom
allocator; introduce pooling or arena strategies only where profiling proves
allocation is a bottleneck, with bounded lifetime and ownership evidence.

## Execution Plane Ownership

Keep language count separate from ownership. C and C++23 own approved
deterministic execution, native tests, inference and audit hot paths. C# owns
interfaces and the single authorized workflow/test orchestration surface. Rust
owns the UI application/state/security/IPC/lifecycle core and native RAG
retrieval. Go owns high-concurrency file, batch and network work. F# owns data
analysis, machine learning and correctness-sensitive complex calculations.
Julia owns specialized numerical research. Native JavaScript ESM with JSDoc is
UI-only. Python holds zero role — it is retired in every domain (B166), with
no residual governance, training, verification or bulk-work path. PostgreSQL
is the sole structured-data authority and Qdrant is
the scoped semantic index; no embedded database may act as authority or fallback.

The canonical runtime path uses the shortest governed native boundary. C/C++ execution does not create or
change governance rules; C# orchestration cannot bypass decision or permission
checks.

## Global Work Scheduler

All asynchronous work must use bounded class budgets rather than independent
unlimited task/thread/process/goroutine/rayon/model-request growth. Classify
work as CPU, IO, DB, VECTOR, MODEL, GPU, or BACKGROUND; size each budget from
physical cores, connection pools, vector limits, model capacity, VRAM, and
measured backpressure. Enforce cancellation, queue bounds, memory limits, and
health evidence at the scheduler boundary.

## Runtime Throughput Patterns

Use a small number of long-lived Rust CPU workers with a bounded global queue,
worker-local queues, and work stealing for document parsing, hashing, chunking,
RAG preprocessing, indexing, search verification, and other many-small-task
workloads. Do not create a thread per task.

Keep models resident by lifecycle state (`HOT`, `WARM`, `COLD`, `EVICTING`,
`LOADING`) and route requests to already-resident models. Reuse mapped weights,
tokenizer, KV/cache and allocator resources; use LRU plus VRAM budget for large
models, and use AutoRelease for cold resources.

RAG retrieval paths run concurrently where independent (keyword, vector,
metadata), then merge → rerank → PostgreSQL verification → context. Qdrant
hits must still be verified by PostgreSQL. Batch embeddings, database inserts,
Qdrant upserts, logs, audit receipts, hashes, metadata lookups, and IPC events;
do not replace batching with more threads.

Use JSON for control messages, typed/binary structures for high-frequency
internal messages, and references (`resource_id`, revision, scope, capability,
hash) for large data. Prefer move over clone, slice/view over copy, mmap over
read-all, and stream over buffer-all. PostgreSQL stores canonical durable state,
lineage, registry, metadata, audit, mutations, and governance receipts; runtime
events use bounded in-memory queues with batched persistence.

Fail fast in the order authentication → scope → capability → schema → quota →
dispatch. Propagate one cancellation token through Go, Rust, DB, RAG, and model
generation. Use bounded L1 process cache, L2 shared local cache, and L3
PostgreSQL/Qdrant authority; cache keys include resource, revision, scope,
model, and config version. Keep startup minimal: core config → IPC → UI ready →
DB pool → scheduler → requested lazy service → asynchronous model warm-up.

## Performance Contracts

The following PERF contracts are mandatory:

- `PERF-01` no unnecessary Python hop on critical paths.
- `PERF-02` CPU-bound work prefers Rust; `PERF-03` high-concurrency I/O prefers Go.
- `PERF-04` all concurrency is bounded; `PERF-05` no unbounded queue, thread,
  goroutine, or task.
- `PERF-06` bulk operations use batching; `PERF-07` large payloads use
  references, streams, or zero-copy paths.
- `PERF-08` models remain resident while hot and never reload per request.
- `PERF-09` independent RAG stages run in parallel; `PERF-10` cancellation is
  end-to-end.
- `PERF-11` caches are bounded, revision-aware, and non-canonical.
- `PERF-12` nonessential services initialize lazily.
- `PERF-13` observability cannot block the critical path; `PERF-14` canonical
  writes are separated from runtime telemetry.
- `PERF-15` optimization decisions require measured p50/p95/p99, CPU, RAM, VRAM,
  context switches, queue depth, and DB round-trip evidence.

## Performance Convergence Order

Do not add unrelated features while performance convergence is in progress.
Execute optimization in this order:

1. remove Python from critical paths through Go/Rust ownership;
2. establish the Global Scheduler, bounded concurrency, and backpressure;
3. implement model residency and switching management;
4. batch and parallelize RAG, SQL, and Git pipelines;
5. optimize zero-copy, serialization, and allocations;
6. add lazy startup and cache hierarchy;
7. apply the final 10–20% only from profiling evidence.

Stages 1–4 have priority. Avoid premature SIMD, handwritten memory pools, and
complex lock-free structures when profiling has not demonstrated a bottleneck.
The convergence rule is: native owners execute bounded work, PostgreSQL owns
structured truth, Qdrant owns scoped vectors, Python stays retired with zero
role, and every operation remains cancellable, observable and governed.


## Codex Read Access (no-Python path)

The local Python runtime is retired; psycopg-based loaders
(codex_repository/codex_official/codex_session) cannot run on this
machine.  The governed C# port in `GPTBridge.CodexPipeline` now owns the
official read entry (A113/A435: session mint, per-request review,
dual-key, scope check, metadata-only audit):

```powershell
$pipe = 'shared-layer\csharp\GPTBridge.CodexPipeline\publish\GPTBridge.CodexPipeline.exe'

# authority state (version, tables, rows, sha256)
& $pipe --authority-state

# official-entry read: open session -> one typed op -> close (audited)
& $pipe --codex-read --actor <sovereign-id> --purpose global-review --scope codex:identity --op identity
# ops: identity | sovereign | sovereigns | provision-exists | provision-text
#      edicts | articles | principles | registry-names | directory-names
#      registry | directory | snapshot | chinese-mirror
# classes: review-session (default) | bounded-machine-lookup
#          | xingcheng-chinese-review (XingCheng sovereign only)

# privileged opens (codex:full / amendment-verification) need a grant:
& $pipe --mint-dual-key --operation codex-open:review-session --primary <sovereign> --secondary <distinct-sovereign> --purpose global-review --scope codex:full
& $pipe --codex-read ... --grant <nonce>   # single-use, replay-proof
& $pipe --revoke-codex-reads               # bump revocation generation
& $pipe --official-sovereign <sid>         # self-declaration read
```

Session/grant state persists in
`governance_rule/execution/audit/codex_entry_state.json`; audit records
append to `codex_read_audit.jsonl` (metadata-only, A435).  Fallback
read-only queries via psql (print only; never persist codex content):

```powershell
& 'C:\Program Files\PostgreSQL\18\bin\psql.exe' <GPTBRIDGE_POSTGRES_DSN> -c "SELECT ... FROM gptbridge_codex.<table>"
```

Maintenance lanes: `--repair-projections` rebuilds the live derived
search/index/manifest projections from authoritative tables;
`--mirror-zh` re-renders the five zh-TW mirror parts into
`governance_rule/codex/` (read-only output).

Current authority row: version 2026-10-02T10:22:07Z (revision 224),
218 tables, 23623 rows. The read-only Chinese mirror remains
non-authoritative.

## Codex Amendment Workflow

Workers **never modify the codex directly**. PostgreSQL
`gptbridge_codex` is the sole authority;
`governance_rule/codex/data/governance_codex.sql` is a pipeline-regenerated
working export (`UpdatePipeline.DatabaseName` — rewritten on every
amendment; stale copies were purged 2026-10-02 and the abandoned 0-byte
`governance_codex.db` was deleted under governor authorization); the
five `governance_codex.zh-TW.part-*.txt` files are read-only
mirrors. All changes go through the governed amendment pipeline:

1. Author a `codex-amendment-request/v2` JSON artifact. Canonical
   format: `governance_rule/execution/audit/convergence/codex-amendment-request-format.md`.
   Required: `problem`, `predecessor` (codex_version + history_head),
   `not_executed: true`, payload (`changes`/`proposed_successors`/
   `proposed_change`). Forbidden: `auto_execute`, `status`,
   `amendment_id` — execution gating lives in
   `main-system/config/automation-flows.json` (`codex-amendment-intake`).
2. Drop it into an intake dir: `main-system/runtime/state/` or
   `governance_rule/execution/audit/convergence/`. Filename must equal
   `request_id`: `codex-amendment-request-<slug>-<yyyymmdd>[-r<n>].json`.
3. `GPTBridge.CodexPipeline` (resident `codex-amendment-intake` flow,
   `auto_execute: true`) advances it: intake → successor staging
   (`candidates/<id>.sql`) → five-sovereign audit → publish. Ledger:
   `main-system/runtime/state/codex-amendments/requests/<id>.json`;
   terminal files are renamed `.executed`/`.rejected`/`.withdrawn`.

Evaluation tool:
`governance_rule/execution/audit/convergence/formal-state-closure-evaluator.ps1`
— run without args for a read-only current-registry evaluation report
(JSON); `-Request` emits the amendment-request payload. Requires
PowerShell (`powershell.exe -File`); there is **no Python installed** on
this machine (`py` launcher exists but no interpreter).

Worker rules for codex work:

- **Dedup before authoring**: check `revision_history` tail and the
  amendment ledger first — the fix may already be executed by another
  worker (incident pattern: findings reported against an older head
  that a concurrent session already amended).
- A104: workers never self-seal; Ed25519 external signatures and
  sealing certificates are human-governor scope only.
- Amendment `changes` propose field-level row updates; closure/state
  rows are rebuilt by the pipeline at the successor version — never
  hand-edit `governance_codex.sql`.
- Honest-closure rule: missing execution evidence stays
  `INCOMPLETE_EVIDENCE`/`PENDING`; never fabricate PASS.

## Open Work Items (as of 2026-10-02)

1. **Machine schema parity — CLOSED via governed restamp (2026-10-02).**
   Governor adopted `SEAL_CANONICAL_V1` (option B of the 09-24 staged
   proposal); amendment `machine-schema-parity-restamp-seal-canonical-v1-20261002`
   executed 11:53Z (rev 237): all 77 `machine_schema_parity_evidence`
   rows restamped to the C#-measured producer hash and
   `machine_schema_registry.parity_status=PASS`. Post-execution probe:
   `canonical_match=77/77`, `producer_validator_pass=77/77`.
2. **Xingcheng model-service runtime smoke DONE (2026-10-02).**
   Evidence: `convergence/xingcheng-model-service-smoke-20261002.json`.
   Governed path: rebuilt `gptbridge-backend` (the 09-30 binary predated
   `a0d76af5b`'s xingcheng manifest scan → first attempt returned
   `TOOL_UNKNOWN`), then `toolbox_start_tool{xingcheng}` → running;
   descriptor `tool_id=xingcheng`,
   `lifecycle_owner=xingcheng/toolhost-model-service`,
   `consumer_policy=csharp-orchestrator-client-only`; `/v1/status`
   no-token → 403, token → 200 (`loaded:false`, lazy serve confirmed);
   `toolbox_start_tool{model-dialogue}` → running. Inference
   cold-start exercised later same day (devin-cli, evidence
   `model-service-smoke-evidence-20261002.json`): first `/v1/infer`
   failed `TOKENIZER_BACKEND_UNAVAILABLE:xcorpus.dll` — the governed
   build never deployed xcorpus.dll next to `xc_modeltool.exe`;
   `tools/build.ps1` now builds+copies the cdylib and the dll is
   deployed in place. After a worker restart (xtok memoizes a failed
   LoadLibrary in a static) infer returned 200 real output (4 tokens,
   512 ms, model_version 4c3a033a8cfecbab). Remaining unexercised:
   `star_chat` end-to-end delivery.
3. **Deferred native lanes stay fail-closed.** `ai-assistant` business
   executor (DeferredExecutor), `file-sorter` native executor,
   `investment-mobile` native entry, and `vaultly`
   (`TOOL_EXECUTOR_PENDING_NATIVE_PORT`) have no production implementation;
   keep them deferred/fail-closed, do not mark ready.
4. **Stale-evidence hazard from resident hosts.** The resident
   `GPTBridge.Automation` host can carry an older in-memory
   `GPTBridge.CodexPipeline` binary and rewrite evidence rows with
   stale logic. After any amendment + rebuild, verify the current
   evidence row's `codex_version`/`version_identity` matches the live
   head and, if needed, run the fresh published binary with
   `--repair-projections` directly.
   As of 2026-10-02 ~19:40 an orphaned `GPTBridge.Automation.exe`
   (parent dead, state files stale since 07:35) was killed; backend
   `gptbridge-backend` restarted ~19:42 with a fresh unified host —
   locks were reclaimed cleanly (stale lock files self-heal).
5. **Other workers' in-flight changes.** `native/resource_governor`
   and `XingchengLearning/ResourceGovernance*` land via separate
   workers; do not sweep them into unrelated commits (path-scoped
   commits only).
6. **xstore metadata-authority takeover CANCELLED (2026-10-02
   decision reversal).** PostgreSQL remains the formal structured
   metadata authority (`gptbridge_xingcheng*` schemas — dataset/job/
   candidate/evaluation/audit rows); `xstore` is scoped to objects,
   snapshots, content hashes and derived indexes only — never a
   metadata/table authority. The earlier "接管中" marks in
   `architecture-xingcheng-{architecture,data,capabilities}.md` have
   been removed; the assessment that xstore has no metadata contract
   surface stands (that absence is now the intended end state, not an
   open gap). No migration work remains here.
7. **Ollama dependency: B154 retired (executed 2026-10-02T11:40Z, rev
   235); implementation removal executed.** Amendment
   `b154-ollama-retirement-20261002` rewrote B154 to
   `RETIREMENT:…/FORBID:ollama-service-activation-or-start|…` and set
   `provision_lifecycle_status=retired`. Implementation removed:
   `native/ollama_service/` deleted (sources pinned file-not-exists via
   `retired_sources.json`), `TeacherCollect.cs` deleted,
   `--teacher-collect` is a fail-closed `TEACHER_LANE_RETIRED` stub,
   `teacher-distillation.json` is a disabled retired stub, the backend
   status payload no longer declares an `ollama` dependency, and
   `startup_manifest.json`/`resident-core.json`/
   `data-architecture-contract.json`/`release-dependencies.json`/
   `DependencyProbes.cs` carry no Ollama probe or activation path.
   Sibling articles B155/A130/B25/C32 — residue convergence
   EXECUTED (rev 238, 2026-10-02T12:40Z): original request rejected
   on stale predecessor, resubmitted as
   `codex-amendment-request-ollama-sibling-residue-20261002-r2` —
   B155/B25/A130/C32(rule+exception)/P113 de-Ollama'd,
   FR-OLLAMA-ON-DEMAND and module_capability_registry 'ollama'
   retired, ollama metadata keys carry retired markers.
   Follow-up EXECUTED (rev 242, 2026-10-02T13:04Z):
   `codex-amendment-request-module-capability-retired-residue-20261002-r4`
   — module_capability_registry rows sub-sovereign-orchestration,
   model-training, module-sqlite and system-rescue stamped
   status/runtime_state/availability=retired (r1-r3 rejected on stale
   predecessor / intake read race).
8. **Codex open evidence gaps block verified release.**
    `postgresql_role_registry` is now populated (48 rows observed live
    2026-10-02, live↔registry delta = 0, evidence
    `postgresql-role-observation-20261002.json`) but every row is still
    `INCOMPLETE_EVIDENCE` — the governed *purpose* of each role is
    unverified, which requires real acceptance evidence through the
    governed pipeline (never hand-edit). `DIR_DATA_SCHEMA_AUTHORITY` =
    `INCOMPLETE_EVIDENCE` / `verified-release-denied` / `open` remains
    unresolved likewise. Fresh evidence
    (`postgresql-governance-evidence-devin-20261002.json`): 20 live
    `gptbridge_*` schemas have zero authority-directory entries
    (incl. `gptbridge_codex`, `gptbridge_xingcheng*`, trading/tool
    schemas); 11 leftover `gptbridge_codex_codex_stage_*` staging
    schemas need governed cleanup; retired-tool roles
    (`system_rescue`/`global_cleaner`/`local_ai`) still exist live;
    `gptbridge_runtime` login holds direct grants on 39 schemas incl.
    test schemas.
9. **No system Python on this host.** `Python313` lacks `python.exe`
    and the `py` launcher finds no install, so the retired
    `python -m governance_rule.execution.audit` entry cannot run.
    Expected (Python lane retired, B166); audit evidence must come from
    the governed C#/native pipeline and the pre-commit hooks.
10. **Governed migration executor now exists; live chain is still
    unapplied.** `shared-layer/csharp/GPTBridge.CodexPipeline/
    MigrationExecutor.cs` implements `sql_migration_executor_contract`
    (`--migration-status` read-only, `--migration-apply --sequence N`,
    fail-closed on every contract gate, append-only hash-chained
    receipts, `pg_try_advisory_lock` global lock). Verified end-to-end
    on `gptbridge_scratch`. Live `gptbridge` still has **0 receipts**
    and 110/148 migration files with no applied objects — do not claim
    migration closure. `--migration-db` is a scratch-only test hook,
    never point it at the governed DB.
11. **Migration registry: restamp + retirement EXECUTED (rev 236,
    2026-10-02T11:50Z); 89-file registration still open.** Amendment
    `sql-migration-registry-convergence-20261002` landed the governor's
    decisions: (a) 15 `migration_source_hash`/`source_hash` rows
    restamped to live file SHA-256 (repair commits `e487bc473`/
    `99ba911f3`) — `--migration-status` now reports `match=45,
    mismatch=0`; (b) seqs 25/37/45/46 `status=retired` in both
    `sql_migration_registry` and `sql_migration_authority_registry`
    (files intentionally deleted in `26c63f79e`; never recreate), with
    successors rewired 26→24, 38→36, 47→44 and pre-state hashes
    re-anchored to the new predecessor's registered target; (c) the 89
    unregistered files (050–148) are inventoried in
    `convergence/migration-unregistered-triage-20261002.json` — all
    classified `active-missing-registration`, plus 2 filename-prefix
    collisions (087×2, 088×2) and 12 absent sequence numbers awaiting
    governor sequence assignment. Still open: downstream
    `target_schema_hash` values are stamped on the pre-retirement
    chain (the retired migrations created real `gptbridge_index.*`
    objects), so the executor chain cannot pass `verify-target-hash`
    past the retired points until a governed full-chain hash
    re-derivation lands; registered hashes also still use the
    governor-side recipe vs executor `OBJECT_MANIFEST_V1`. The bundled
    `manual-amendment-request-sql-migration-registry-reconciliation-20261002.json`
    remains unsubmitted (intake glob requires the
    `codex-amendment-request-` prefix; its restamp/retire portions are
    now superseded, its 89 precomputed registrations still pending
    governor decision on the collision assignments).
12. **Parity extractor reconciled in C#; current-generation evidence remains open.** `MachineSchemaParity.cs` ports
    `SEAL_CANONICAL_V1` byte-exact (verified against a hand-computed
    Python-semantics hash for AUDIT_EVENT) — verbs `--schema-parity`
    (full probe) and `--parity-descriptor <code>` (diagnostic).
    Current evidence `machine-schema-parity-probe-20261002.json`:
    77/77 producer=validator PASS, 0/77 canonical match. A second,
    oracle-verified port `SemanticHashToolchain.cs` (recovered Python
    source at `675fa045b^`, byte-parity proven incl. `default=str`,
    ensure_ascii escapes and `|`-split fallback) now exposes
    `BuildDescriptor`/`ComputeProducerHash`/`EvaluateRow` as pure
    functions. `MachineSchemaParity.cs` now delegates descriptor/hash/
    row evaluation to the shared port (typed values no longer become
    null). Float serialization uses the oracle notation thresholds
    (1e-4 / 1e16), preserves negative zero and shortest-round-trip
    digits. Regression: `dotnet run --project shared-layer/csharp/
    GPTBridge.SemanticHash.Tests -c Release` (19 assertions passed).
    The fresh build's live probe covers 77/77 rows, producer=validator
    77/77, canonical match 77/77 after the parallel governed restamp.
    Publish unblocked (2026-10-02, worker:devin-desktop): the locking
    watcher exited with the orphaned host; the 20:30 Release build was
    deployed to `publish/` and verified live — `--authority-state`
    reports rev 238 (12:40:26Z), `--schema-parity` 77/77
    producer=validator / 77/77 canonical match, and
    `--repair-projections` rebuilt derived projections onto the
    current head (236 authority rows, 906 documents, 742 fts).
    The live registry now reports 77 VERIFIED rows and hash matches,
    but persisted validation evidence still anchors the predecessor
    2026-10-02T11:49:37Z. Current-generation evidence closure remains
    pending (claimed: codex/schema-evidence-generation); neither hash
    parity nor registry labels certify release.

13. **Production Closure directive ��0�V��148 in force; phase-1
    foundations landed (2026-10-02, worker:devin-cli).** New phase:
    no new capabilities/architectures/runtimes/formats/governance �X
    prove the existing system runs long, survives faults, and hands
    off generations safely. `ProductionClosure.cs` lands
    `star-production-certification/v1` (��4 12-axis matrix, ��5
    NOT_RUN/RUNNING/PASS/FAIL/BLOCKED �X SKIPPED is not a state),
    `star-production-health/v1` lightweight receipt (��122/��123), the
    ��119 state machine (DEVELOPMENT��CANDIDATE��CERTIFYING��
    PRODUCTION_READY��ACTIVE��DEGRADED/RECOVERY_REQUIRED��RETIRED),
    ��1 `PRODUCTION_CLOSURE_FREEZE` marker (ACTIVE since 11:58Z �X
    mutating surfaces should call `ProductionClosure.FreezeGuard`),
    ��2 candidate pin (single-generation, refused once pinned) gated
    on ��3 prerequisites (`--production-prereqs` derives live from the
    latest release-gate report; all eight gates currently
    NOT_EVALUATED �X candidate pin correctly refuses). Verbs:
    `--production-certification`, `--production-prereqs`,
    `--production-candidate --generation <g>`,
    `--production-certify --axis <a> --state <s> --evidence <f>`,
    `--production-state`, `--production-freeze --on|--off`,
    `--production-health`. PASS on an axis requires an existing
    evidence file (sha256 pinned); state store:
    `xingcheng/runtime/state/production-certification.json`. Open:
    soak harness ��6-��10 (8/24/72h), crash/fault batteries ��15-��20,
    ��40-��50, ��85-��91, generation succession ��92-��105, release bundle
    ��126-��127 �X all gated on a pinned candidate, which is gated on ��3
    prerequisites reaching PASS.

    First live gate evidence (release-gate `gate-20261002-120127.json`):
    prereqs derive 4 PASS (capability-consistency, capability-delta
    regression, resource-contract, cuda-probe), 1 FAIL (native-only �X
    production-scope blocking findings: onnxruntime refs in
    `xcm_silicon.h` ��2, `LoadLibraryA("nvml.dll")` in
    `cuda_kernels.cpp`, Npgsql+System.Management nuget in the
    XingchengLearning csproj, Npgsql source-ref in `Pg.cs`, and 8
    third-party cargo crates in xstore/xcorpus manifests) and 3
    NOT_EVALUATED (architecture-drift SKIPPED �X bundle-bound step;
    capability-floors �X no capability evidence yet; provenance �X no
    live surface). SKIP is mapped to NOT_EVALUATED, never PASS and
    never FAIL. Note the gate run itself had environmental FAILs to
    re-run cleanly: build-modeltool LNK1104 (worker holds the exe),
    build-xc-learning file lock (concurrent run), self-test +
    dataset-retention TRANSFORMER_TRAINING_SNAPSHOT_SCOPE_DENIED.
    Phase-2 landed: `ProductionSoak.cs` �X `star-runtime-soak-sample/v1`
    sampler (��8: rss/commit/paged bytes, threads, handles, /v1/status
    probe latency+VRAM/KV columns when the service reports them) and
    `star-production-soak-analysis/v1` (��9/��10 head-vs-tail slope
    verdict: BOUNDED_WARMUP / FLAT / UNBOUNDED_GROWTH / TARGET_EXITED /
    INSUFFICIENT_SAMPLES; RSS alone never convicts). Verbs:
    `--production-soak --pid N [--seconds] [--interval-ms] [--port]
    [--token-file]`, `--production-soak-analyze --file <jsonl>`.
    Verified live against the toolhost worker (probe 200 via
    `X-GPTBridge-Session-Token`).
