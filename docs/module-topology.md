# Module Topology — A534 Alignment

## Execution Layer Modules (A534)

A534 replaces A66/A533: the module-execution topology is main-system
internal execution services plus exactly seven independent tools:

```
local-model + model-dialogue + ai-assistant + investment-mobile +
ai-collaboration + file-sorter + vaultly
```

Retirement notes:

- `global-cleaner` was retired, non-executable, and its folder deleted
  (E14, 2026-09-22). Its identity, lineage and audit records remain
  registered as evidence; automatic cleanup is owned in-process by the
  main-system internal cleanup service.
- `system-rescue` was retired (codex `retired`, 2026-09-27); its
  `Standalone tools/system-rescue/` folder was removed and its records
  remain as evidence under `main-system/data/retired-system-rescue-archive`
  and `main-system/data/automatic-repair/system-rescue`.
- `xingcheng` is an independent privileged institution, outside the
  independent-tool set.

## Physical Folder Layout

Independent tools live under `Standalone tools/`. The retired
global-cleaner folder was deleted (E14, 2026-09-22); its identity,
lineage and audit records remain registered as evidence.

| Module | Folder | Manifest | Status |
|---|---|---|---|
| local-model | `Standalone tools/local-model/` | `manifest.json` | `on-demand` |
| model-dialogue (star-chat) | `Standalone tools/model-dialogue/` | `manifest.json` | `on-demand` |
| ai-assistant | `Standalone tools/ai-assistant/` | `manifest.json` | `on-demand` |
| investment-mobile | `Standalone tools/investment-mobile/` | `manifest.json` | `on-demand` |
| ai-collaboration | `Standalone tools/ai-collaboration/` | `manifest.json` | `on-demand` |
| file-sorter | `Standalone tools/file-sorter/` | `manifest.json` | `on-demand` |
| vaultly | `Standalone tools/vaultly/` | `manifest.json` | `on-demand` |

`Standalone tools/` also hosts internal-service implementation folders
that are not independent tools and carry no `manifest.json`:
`business-logic-csharp`, `business-logic-fsharp`, `process-metrics-csharp`
(internal services), `searchd-go`, `vectord-rs`, `ragd-rs` (native
search/vector/RAG services), and `julia-compute` (registered retired in
the codex 2026-10-02; folder still present pending removal).

## model-dialogue Nested Relationship

B123/B125 keep `model-dialogue` as one of the seven independent tools.
Its folder is a top-level directory under `Standalone tools/`; its
companion surface `star-chat` stays physically nested under
`model-dialogue/`. The manifests now declare the separated
topology directly:

- `model-dialogue/manifest.json` declares:
  - `id: "model-dialogue"`
  - `host_tool_id: "model-dialogue"` (self-hosted, B125)
  - `runtime_owner_tool_id: "model-dialogue"`
  - `physical_owner_root: "model-dialogue"`
  - `main_system_independent_tool: true`

- `model-dialogue/star-chat/manifest.json` declares
  `host_tool_id: "model-dialogue"` and `companion_tool: true` — it is
  model-dialogue's companion surface, not a member of the tool roster.

`physical_owner_root` expresses directory containment only; governance
parentage and service identity come from the manifest identity fields
and the architecture registry, never from the path (B123 forbids
deriving governance parentage from the physical path).

## xingcheng Separation (B81/B123)

`xingcheng` is an **independent privileged institution** — a separate
local native-model service, not a tool, not a companion of any tool:

- `xingcheng/xingcheng/manifest.json` declares
  `service_kind: "independent-privileged-institution"`,
  `independent_tool: false`, `main_system_independent_tool: false`,
  `runtime_owner_tool_id: "xingcheng"`, and no `host_tool_id` /
  `companion_*` fields. It carries no tool card and is not a child of
  `local-model` or `model-dialogue`.
- `local-model/manifest.json` no longer declares a `companion_tools`
  entry for xingcheng; local-model neither owns, hosts, nor controls
  the xingcheng service.
- The institution lives at the repository-root enclave `xingcheng/`
  (moved out of `local-model`, 2026-10-02). All xingcheng-owned data
  (identity, memory, weights, corpus, checkpoints, repair knowledge,
  runtime records) stays inside the registered domain root
  `xingcheng/xingcheng/` (institution root); the former
  `model-dialogue/xingcheng/` star directory is retired (2026-10-01).
  Backups resolve in-domain (`xingcheng/runtime/backups`) per the
  `backup-outside-owner-boundary` prohibition.

## Infrastructure (Non-Module) Folders

| Folder | Role | Codex Basis |
|---|---|---|
| `governance_rule/` | Codex governance (PostgreSQL authority) | A76/E56 |
| `shared-layer/` | Information layer (channel owner) | A153/E50 |
| `main-system/` | Platform orchestrator | A59/A151 |
| `launcher/` | Startup state stamp (launcher lives under `main-system/launcher/`) | A129 |
| `native/` | Native compute core & resource governor implementation | native-compute-core / resource-governor |

### launcher/ Detail

`launcher/` is **not an execution-layer module** — it has no `manifest.json`
and no runtime entry. It is a **startup state storage location**.

Per A129 (startup-sub-sovereign), the launcher (`main-system/launcher/`,
`GPTBridge.Bootstrap.exe` / `GPTBridgeLauncher`) performs:
1. Environment loading
2. Runtime checks (PostgreSQL probing; Qdrant and Ollama are retired dependencies — Ollama retired by the B154 retirement amendment, executed 2026-10-02 rev 235)
3. Governance audit
4. Startup gate decision

The launcher writes its state to `main-system/launcher/state/`:
- `startup-journal.jsonl` — append-only event log of startup orchestration

(The repository-root `launcher/` folder only retains
`state/requirements.stamp`; the launcher implementation and runtime
state live under `main-system/launcher/`.)

The validated state is then handed to the backend through:
- `GPTBRIDGE_STARTUP_STATE` — the READY/DEGRADED/RECOVERY string
- `main-system/launcher/state/orchestrator-report.json` — full service report

The startup sovereign (A129) owns startup orchestration; `launcher/` is
the physical state artefact location, not a sovereign or module.

## Non-Execution Folders

| Folder | Purpose |
|---|---|
| `.devin/` | Devin CLI configuration |
| `.smallcode/` | Smallcode configuration |
| `.venv/` | retired — Python virtual environment removed (B166) |
| `.vs/` `.vscode/` | IDE configuration |
| `docs/` | Documentation |
| `scripts/` | Utility scripts |

## Core System Governance Modules — retired (B166)

The former Python `main-system/src-core/core_system/` module groups
(previously 53 modules across 12 groups registered under codex module
registry v1.02000) were retired together with the Python lane
(B166, 2026-09-29). The directory no longer exists;
`main-system/src-core/` now carries only `ipc/`, `main.json` and
`utils/`, and the codex no longer registers a `core_system` module set.
The A181–A202 governance responsibilities continue under the
owner-language toolchain (C# / F# / native) and the audit pipeline;
the retired groups remain in Git history and audit artifacts.
