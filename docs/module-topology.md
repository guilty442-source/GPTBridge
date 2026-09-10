# Module Topology — A66 Alignment

## Execution Layer Modules (A66)

A66 defines 9 execution-layer modules:

```
local-model + model-dialogue + ai-assistant + investment-mobile +
ai-collaboration + file-sorter + global-cleaner + vaultly + system-rescue
```

## Physical Folder Layout

| Module | Top-level Folder | Manifest | Status |
|---|---|---|---|
| local-model (xingcheng) | `local-model/` | `local-model/manifest.json` | `running` |
| model-dialogue (star-chat) | `local-model/model-dialogue/` | `local-model/model-dialogue/manifest.json` | `stopped` |
| ai-assistant | `ai-assistant/` | `ai-assistant/manifest.json` | `stopped` |
| investment-mobile | `investment-mobile/` | `investment-mobile/manifest.json` | `stopped` |
| ai-collaboration | `ai-collaboration/` | `ai-collaboration/manifest.json` | `stopped` |
| file-sorter | `file-sorter/` | `file-sorter/manifest.json` | `stopped` |
| global-cleaner | `global-cleaner/` | `global-cleaner/manifest.json` | `stopped` |
| vaultly | `vaultly/` | `vaultly/manifest.json` | `stopped` |
| system-rescue | `system-rescue/` | `system-rescue/manifest.json` | `stopped` |

## model-dialogue Nested Relationship

A66 lists `model-dialogue` as a peer of `local-model`, but physically
`model-dialogue/` is nested under `local-model/model-dialogue/`.

This is intentional and documented in both manifests:

- `local-model/manifest.json` declares:
  - `physical_owner_root: "local-model"`
  - `companion_tools: [{"id": "star-chat", "path": "model-dialogue", "independent_only_in": "main-system"}]`

- `local-model/model-dialogue/manifest.json` declares:
  - `id: "star-chat"`
  - `host_tool_id: "xingcheng"`
  - `runtime_owner_tool_id: "xingcheng"`
  - `physical_owner_root: "local-model"`
  - `canonical_source_root: "local-model/model-dialogue"`
  - `main_system_independent_tool: true`
  - `independent_only_in: "main-system"`

### Rationale

`local-model` is the **physical owner root** for the local-model platform.
`model-dialogue` (star-chat) is a **companion tool** that:

1. Is physically hosted under `local-model/` (shares runtime, cache, backup)
2. Operates independently only within the `main-system` context
3. Uses `xingcheng` as its host/runtime owner
4. Has its own `manifest.json` with independent permission profile

A66's peer-level listing reflects **logical independence** (separate tool
identity, separate manifest, separate permission scope), not physical
folder placement.

## Infrastructure (Non-Module) Folders

| Folder | Role | Codex Basis |
|---|---|---|
| `governance_rule/` | Codex storage (immutable SQLite) | A76/E56 |
| `shared-layer/` | Information layer (channel owner) | A153/E50 |
| `main-system/` | Platform orchestrator | A59/A151 |
| `launcher/` | Startup state storage | A129 |

### launcher/ Detail

`launcher/` is **not an execution-layer module** — it has no `manifest.json`
and no runtime entry. It is a **startup state storage location**.

Per A129 (startup-sovereign), the launcher (start.ps1) performs:
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
| `.venv/` | Python virtual environment |
| `.vs/` `.vscode/` | IDE configuration |
| `docs/` | Documentation |
| `scripts/` | Utility scripts |
