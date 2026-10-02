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
- `system-rescue` is a main-system internal service only, not an
  independent tool.
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
| system-rescue (internal service) | `Standalone tools/system-rescue/` | `manifest.json` | `on-demand` |

## model-dialogue Nested Relationship

B123/B125 keep `model-dialogue` as one of the seven independent tools
while its folder stays physically nested under
`model-dialogue/`. The manifests now declare the separated
topology directly:

- `model-dialogue/manifest.json` declares:
  - `id: "model-dialogue"`
  - `host_tool_id: "model-dialogue"` (self-hosted, B125)
  - `runtime_owner_tool_id: "model-dialogue"`
  - `physical_owner_root: "local-model"` (physical containment only)
  - `main_system_independent_tool: true`

- `model-dialogue/star-chat/manifest.json` declares
  `host_tool_id: "model-dialogue"` — it is model-dialogue's companion
  surface, not a member of the tool roster.

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
- All xingcheng-owned data (identity, memory, weights, corpus,
  checkpoints, repair knowledge, runtime records) stays inside the
  registered domain roots `xingcheng/xingcheng/` (institution root)
  and `model-dialogue/xingcheng/` (star directory);
  backups resolve in-domain (`xingcheng/runtime/backups`) per the
  `backup-outside-owner-boundary` prohibition.

## Infrastructure (Non-Module) Folders

| Folder | Role | Codex Basis |
|---|---|---|
| `governance_rule/` | Codex governance (PostgreSQL authority) | A76/E56 |
| `shared-layer/` | Information layer (channel owner) | A153/E50 |
| `main-system/` | Platform orchestrator | A59/A151 |
| `launcher/` | Startup state storage | A129 |

### launcher/ Detail

`launcher/` is **not an execution-layer module** — it has no `manifest.json`
and no runtime entry. It is a **startup state storage location**.

Per A129 (startup-sub-sovereign), the launcher (start.ps1) performs:
1. Environment loading
2. Runtime checks (PostgreSQL/Qdrant/Ollama probing)
3. Governance audit
4. Startup gate decision

The launcher writes its state to `launcher/state/`:
- `startup-journal.jsonl` — append-only event log of startup orchestration

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

## Core System Governance Modules (A181–A202)

The `main-system/src-core/core_system/` directory contains governance
implementation modules registered in the codex `module_registry` table
(v1.02000).  Each module group is split into submodules to comply with
A430/E160 source-size limits (≤3 public entrypoints, ≤12 authored
callables, ≤500 effective lines per module).

| Module Group | Codex Basis | Submodules | Roles |
|---|---|---|---|
| `active_release` | A181/E156, A182/E157 | 7 | facade, types, persistence, ledger, verify, status, mismatch |
| `tool_separation` | A184/E159 | 5 | facade, types, verify, signal, aggregate |
| `source_size` | A430/E160 | 6 | facade, types, report, measure, verify, signal |
| `view_access` | A186/E161 | 4 | facade, types, verify, signal |
| `validation_chain` | A187/E162 | 4 | facade, types, verify, signal |
| `sovereign_collaboration` | A188/E163 | 4 | facade, types, verify, signal |
| `xingcheng_channel` | A189/E164 | 4 | facade, types, verify, signal |
| `governed_startup` | A191/E166, A192/E167 | 4 | facade, types, verify, signal |
| `startup_lifecycle` | A193–A196/E168–E171 | 5 | facade, types, verify, signal, sync |
| `third_party_governance` | A197–A199/E171–E173 | 4 | facade, types, verify, signal |
| `root_containment` | A201–A202/E175–E176 | 4 | facade, types, verify, signal |
| `toolbox_process` | A184/E159 | 2 | facade, persistence |

**Total**: 53 registered modules across 12 groups.

Each facade module re-exports all public names from its submodules,
preserving backward-compatible `__all__` exports.  Submodule roles:

- **facade** — re-export shim, no logic
- **types** — constants and dataclasses
- **verify** — verification functions
- **signal** — information-layer signal production
- **persistence** — durable transactional operations
- **ledger** — append-only history
- **status** — observability and UI status
- **mismatch** — version mismatch classification
- **measure** — source size measurement
- **report** — size violation report dataclasses
- **aggregate** — composite verification
- **sync** — projection and window host checks
