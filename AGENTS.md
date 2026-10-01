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

## 星澄 Self-Learning & Automatic Upgrade

> Normative authority: Codex D131。
> Native lane landed (B167/B38 successor): `GPTBridge.XingchengLearning`
> (`xc-learning.exe`, C#) at
> `Standalone tools/local-model/src/backend/csharp/GPTBridge.XingchengLearning`
> — orchestration, governed interfaces and dataset/eval gates in C#; model
> execution stays in the native C++ lane (`xingcheng_trainer.exe` +
> `xc_modeltool.exe`), reached only through audited subprocesses.
> Production scheduling is unchanged — cycles run inside the
> xingcheng tool process via the governed system channel.
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
   (`star-native-eval-dialogue-20260921-125054` perplexity/tps gate **plus**
   `star-capability-suite-20260925-084920` per-category regression gate — zh-TW/en/
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
& "Standalone tools\local-model\src\backend\csharp\GPTBridge.XingchengLearning\publish\xc-learning.exe" --tool-root "Standalone tools\local-model" --status
& "Standalone tools\local-model\src\backend\csharp\GPTBridge.XingchengLearning\publish\xc-learning.exe" --tool-root "Standalone tools\local-model" --run-once
& "Standalone tools\local-model\src\backend\csharp\GPTBridge.XingchengLearning\publish\xc-learning.exe" --tool-root "Standalone tools\local-model" --run-once --force
& "Standalone tools\local-model\src\backend\csharp\GPTBridge.XingchengLearning\publish\xc-learning.exe" --tool-root "Standalone tools\local-model" --disable

# governed end-to-end smoke (scratch model; never touches the pinned bundle):
& "...\publish\xc-learning.exe" --tool-root "Standalone tools\local-model" --self-test
```

Implementation: `GPTBridge.XingchengLearning` (C#) —
`SelfLearning.cs` (cycle + gates), `Collectors.cs` (role DB),
`SftDataset.cs` (SFT/DPO snapshot bridges), `Repository.cs`
(PostgreSQL `gptbridge_xingcheng` schema + audit chain),
`JobExecutor.cs` + `NativeTools.cs` (native subprocess lane),
`Evaluation.cs` (`xc_modeltool eval`/`capability` gates),
`Lifecycle.cs` (`star-model-lifecycle/v1`), `Retention.cs`; native
execution: `infrastructure/native_transformer/training/xingcheng_trainer.exe`,
bridge: `infrastructure/native_transformer/tools/xc_modeltool.exe`.
The retired Python `self_learning*.py`/`training_job_executor.py` are
interface documentation only — never execution.

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

> Normative authority: Codex C17/C18。
> Native lane landed (B167/B38 successor): `Retention.cs` inside
> `GPTBridge.XingchengLearning` (`xc-learning.exe`, C#) — same policy,
> same fail-closed boundary rules as the retired Python lane.
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
& "Standalone tools\local-model\src\backend\csharp\GPTBridge.XingchengLearning\publish\xc-learning.exe" --tool-root "Standalone tools\local-model" --retention
& "Standalone tools\local-model\src\backend\csharp\GPTBridge.XingchengLearning\publish\xc-learning.exe" --tool-root "Standalone tools\local-model" --retention --apply
& "Standalone tools\local-model\src\backend\csharp\GPTBridge.XingchengLearning\publish\xc-learning.exe" --tool-root "Standalone tools\local-model" --retention --status
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
   (`xingcheng/runtime/state/generation/migration-*.json`) recording
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
   `runtime/settings/native-engine.json`, flips
   `state/generation/state.json` ACTIVE_GENERATION.
5. `--gen-purge` (dry-run unless `--apply`): deletes predecessor
   executable artifacts — unreferenced bundles and retired weight
   versions — never the target, the pinned checkpoint, or any
   lifecycle-owned active path. The manifest migrates forward with
   the new generation, preserving lineage after predecessor
   deletion.

```powershell
$X = "Standalone tools\local-model\src\backend\csharp\GPTBridge.XingchengLearning\publish\xc-learning.exe"
& $X --tool-root "Standalone tools\local-model" --gen-begin --target v28 --weights <bundle|ckpt> --weight-method direct
& $X --tool-root "Standalone tools\local-model" --gen-record --manifest <id> --domain personality --status migrated --migrated 8
& $X --tool-root "Standalone tools\local-model" --gen-certify --manifest <id> [--suite <suite.json>]
& $X --tool-root "Standalone tools\local-model" --gen-promote --manifest <id>
& $X --tool-root "Standalone tools\local-model" --gen-purge --manifest <id> [--apply]
& $X --tool-root "Standalone tools\local-model" --gen-status [--manifest <id>]
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

- Level 2 request trace (`xingcheng/runtime/logs/capability-trace.jsonl`):
  `request_id`, `intent`, `service_expert`, `model_generation`,
  `architecture_generation`, `router_layers[]`
  (`layer_id`/`router_type`/`selected_neural_experts`/`shared_expert_used`),
  `tool_used`, `rag_used`, `final_result`, `capability_eval`.
- Level 1 expert result
  (`xingcheng/runtime/logs/capability-results.jsonl`):
  `capability`, `status` (pass/fail/degraded/skipped), `evidence`,
  `confidence`, `source`, `failure`, `fallback`, `trace_id`.

```powershell
& $X --tool-root "Standalone tools\local-model" --trace-record --trace <file.json>
& $X --tool-root "Standalone tools\local-model" --cap-record --result <file.json>
& $X --tool-root "Standalone tools\local-model" --trace-status
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
& $X --tool-root "Standalone tools\local-model" --taxonomy          # 軸表
& $X --tool-root "Standalone tools\local-model" --core-contract    # star-model-core/v1
& $X --tool-root "Standalone tools\local-model" --axis-checks      # §42 電池
& $X --tool-root "Standalone tools\local-model" --version-dimensions
```

Implementation: `GPTBridge.XingchengLearning/ArchitectureTaxonomy.cs`、
`AxisChecks.cs`；feature registry 的 `primary_axis` 由
`FeatureCatalog.FeatureDict` 經 taxonomy `Classify` 派生。

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
  `xingcheng/runtime/logs/decision-trace.jsonl`（probabilities /
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
& $X --tool-root "Standalone tools\local-model" --system1-checks     # §44 電池
& $X --tool-root "Standalone tools\local-model" --typed-decision-validate --file <f.json>
& $X --tool-root "Standalone tools\local-model" --cognition-route --file <f.json>
& $X --tool-root "Standalone tools\local-model" --router-stability --file <f.json>
& $X --tool-root "Standalone tools\local-model" --trajectory-validate --file <f.json>
& $X --tool-root "Standalone tools\local-model" --reward-gate --file <f.json>
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
& xc-learning.exe --tool-root "Standalone tools\local-model" --cuda-plane-checks
& xc-learning.exe --tool-root "Standalone tools\local-model" --precision-policy
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
`Standalone tools/local-model/xingcheng/`, `STAR_DIRECTORY` =
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
`xingcheng/runtime/devin/` so scratch work also stays in-boundary.

## 星澄 Training GPU Gate & Auto-Release

> Normative authority: Codex B44/B16。
> Retired lane (B166/B167/B38): the Python classes/modules referenced
> below (`TrainingJobExecutor`, `gpu_coordinator`, `auto_release.py`,
> `NativeTransformerEngine`, `chat_foundation_dataset.py`) are removed;
> the policy contracts remain binding on their native successors.
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
Provider chain is driven by `runtime/settings/web-search.json`
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
(`EmptyWorkingSet`).  Actions revert after ~5 calm minutes.  Protected:
Windows system processes, security software (including the user's antivirus)
and the governor itself.

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
(default `medium`) is the highest mode auto-mode may select, so foreground /
user work always keeps machine headroom; `power_saving_schedule`
(22:00–07:00) forces `sleep` at night.  Control law: responsiveness strain or
machine overload → `low` immediately (urgent, cooldown-exempt); worker demand
+ machine headroom → upgrade after `streak_up` evaluations, clamped to
`ceiling`; downgrades need `streak_down` evaluations plus `cooldown_s`.
Manual mode selection via `app:set-resource-mode` sets `auto_mode=false`
(user intent wins).  Advisor state persists in
`main-system/runtime/state/resource-mode-advisor.json`; mode switches append
to `runtime/state/resource-mode-audit.jsonl` (same ledger the Rust backend
writes for manual changes).

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

Current authority row: version 2026-09-29T05:25:52Z, 218 tables,
23159 rows. The read-only Chinese mirror remains non-authoritative.
