# MainConvergenceMergePlan — Native Production Convergence II

Canonical target: **main worktree** (`E:\GPTBridge`). Devin worktree
(`.worktrees\devin`) is validation-only evidence. No branch merge, no
main rollback. Only devin capabilities that are (a) verified and (b)
absent from main get minimal ports. Main's taxonomy / System-1 / Scale /
Efficiency / 42-mode ModelTool surfaces stay authoritative.

## Baseline (main, verified this round)

- `HybridCausalDecoder` / `xc-fused-1` / `XCN10`
- Trainer: 16 individual probes — all present, now with `--probe-all`
  emitting `star-trainer-probe-report/v1` (**ported, 16/16 PASS**)
- `axis-checks` 10/10, `SystemOne` 20/20, `CommunityChecks` 16/16
- FeatureCatalog 58 features / 0 failures, self-test ok
- `xc_modeltool`: 42 modes (moe-analyze, memplan, parity, precision,
  spec-probe, probe-cuda, pd-pipeline-bench, rag-prefix-bench, …)

## Port matrix (devin → main)

| # | Capability | Devin source | Main target | Strategy |
|---|---|---|---|---|
| 1 | `--probe-all` + `star-trainer-probe-report/v1` | `training/xct_canon.h` (`probe_fnv`, `probe_all`), `xingcheng_trainer.cpp` dispatch | same paths | **DONE** — verbatim port; added `<io.h>`/`<sstream>`; 16/16 PASS |
| 2 | `XingchengConvergenceGate` (unique release gate) | `csharp/.../ConvergenceGate.cs` | new `ConvergenceGate.cs` in main | Port; rebind steps to main batteries (`AxisChecks`, `SystemOne`, `CommunityChecks`, `LangCheck`, `FeatureCatalog`, `BundleProvenance`, `Lifecycle`) + `--probe-all` + 42-mode names |
| 3 | §39 repo-level convergence checks | `ConvergenceChecks.cs` (24 checks incl. repo block) | extend main check surface | Port repo-level block only (single runtime/generation owner, architecture contract, active singleton, lineage, orphan artifacts, forbidden language, training-frozen, XCN writer ≤10, state version); main's axis/community batteries already cover their domains |
| 4 | Failure taxonomy + pool | `RuntimeCapabilities.cs` (`FailureTaxonomy`), `FailurePool.cs` | same names in main | Port; wire failure-pool write into `EvalCoordinator` (main's eval-plane owner) failure path |
| 5 | `InstructionRecovery` (`SINGLE_CAPABILITY_RECOVERY`, Instruction-Following first, 100M parity) | `InstructionRecovery.cs` | same name in main | Port; adapt to main APIs (`BundleProvenance`, `Lifecycle`, `SelfLearning`/`Policy`) — devin copy already fixed for `Take`/Compute-arity/prov-cast; re-fix against main surface |
| 6 | `ThinkingPolicy` facade | `ThinkingPolicy.cs` over `ReasoningRuntime` | same name over main's `ReasoningRuntime` | Port thin facade only (OFF/AUTO/LOW/MEDIUM/HIGH → `think_steps`/`branches`); do not duplicate runtime |
| 7 | Hybrid KV/branch fork fix | `cpp/src/engine_state.h` (`kv_copy_slot` skips non-KV DeltaNet layers), `engine_thinking.h` (branch fork copies `lin_states_`) | same files in main | Port both hunks — real engine bug on `xc-fused-1`; verified via `native-thinking-eval` |
| 8 | BF16 export + load | `xc_modeltool.cpp` export `--quant bf16` (RNE fp64→bf16); `engine_weights.h` bf16 dequant branch | same | Port to main's `export-bundle` option + loader |
| 9 | Bundle-vs-bundle precision parity | `mode_precision_parity --ref-bundle` (logit/top1/top5/gen/router agreement + DeltaNet `state_drift`) | extend main's `parity` mode | Add `--ref-bundle` option — no new mode |
| 10 | `star-moe-trace/v1` unified MoE trace | `analyze_router` + `mode_router_analyze`; `MoeTraceLayer` fields (weights/entropy/shared-gate) | main's `moe-analyze` + engine trace struct | Port capture + emission into existing mode |
| 11 | `star-memory-report/v1` | `plan_memory` (thinking_state, cuda_workspace, idle/prefill/decode/thinking peaks) | main's `memplan` | Port schema fields into existing mode |
| 12 | `NativeSpeculativeDecoder` §11 API (enabled only with bound drafter; `SPECULATIVE_DECODER_DISABLED` fail-closed) | `tools/xcm_capability.h` | main tools dir (fold into existing header e.g. `xcm_runtime.h` or keep `xcm_capability.h`) | Port API + `spec-verify` surface into main's `spec-probe` mode |
| 13 | `cuda-parity-all` (5 lanes vs CPU ref; fail-closed w/o CUDA) | `mode_cuda_parity_all` | extend main's `probe-cuda` | Add `--parity-all` option — no new mode |
| 14 | `native-thinking-eval` (`star-native-thinking/v1`) | `mode_native_thinking_eval` on `generate_thinking` | extend existing mode (`capability` or `eval`) | Add `--thinking` option — no new mode |
| 15 | Native state envelope v2 | `engine_state.h` `NativeStateHeader` (`state_version`, `model_hash`, typed `bind_error`) | same file | Port header + validation |
| 16 | GenerationMigration §33/§30 | purge keep-set = all lifecycle artifact paths + sibling manifests (`GENERATION_ARTIFACT_STILL_REFERENCED`); post-promote verify (`GEN_POST_PROMOTE_VERIFY_FAILED`) | main `GenerationMigration.cs` | Port both hunks (main has purge, lacks both guards) |
| 17 | §22/§23 quarantine | `FeatureCatalog` `NON_CANONICAL_EXPERIMENTAL`/`TRAINER_ONLY_EXPERIMENTAL` + CSA/MLA/lb-bias rows; `engine_weights.h` forged-`xc-fused-1` fail-closed | same | Port statuses/rows + loader guard |
| 18 | `ModelToolModeRegistry` | devin `star-mode-registry/v1` (29 modes) | new registry over main's 42 modes, categories MODEL/TRAINING/STATE/CACHE/PRECISION/CUDA/EXPERT/SCALE/RAG/EVAL/PROVENANCE; unregistered → `MODE_NOT_REGISTERED` | Fresh build on main's dispatch table |
| 19 | Dataset/retention invariants + lifecycle deep-copy regression | devin gate steps | main `ConvergenceGate` + `Lifecycle`/`Retention` | Add gate steps: `(content_sha256, snapshot_sha256)` identity, snapshot protection, dedup/new-row, unreadable-registry preserve; `lifecycle-succession-cycle-check` (serialize→deserialize→resave→rollback→retire, no live `succeeded_from` ref) |

## Explicitly NOT ported

- Devin's 29-mode names — main's 42-mode registry is canonical; devin
  capabilities merge into existing main modes via options.
- Devin `EvalPlane`/`AgentRuntime`/`PersonaRuntime` variants — main has
  its own (`EvalCoordinator`, `LongHorizonTasks`, `PersonaRuntime`); only
  missing behavior gets ported, not the classes.
- Any new mode that an option can cover; new architecture axes; new
  checkpoint versions.

## Validation order per port

1. Build the touched TU (trainer/modeltool/C# project).
2. Run the capability once against a known fixture.
3. Re-run `--probe-all` + self-test + axis/system1/community batteries.
4. After gates land: run `XingchengConvergenceGate` from main.
