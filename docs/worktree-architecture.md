# Git Worktree Architecture — Branch-to-Module Mapping

Per A53/E39 Git operation tiers and the centralized, layered, recoverable local
version service, the repository uses Git worktrees as the layering mechanism.

## Worktree Layout

```
E:\GPTBridge                       main        (stable release)
E:\GPTBridge-worktrees\
├─ local-model                     local-model (local model development)
├─ rag                             rag         (RAG four-sub-architecture)
├─ ui                              ui          (interface layer)
└─ git                             git         (Git governance tooling)
```

## Branch-to-Module Mapping

### main (E:\GPTBridge)

Stable release branch. All merged, verified code.

| Module | Path | Sovereign |
|---|---|---|
| Governance Codex | `governance_rule/codex/` | governance-authority |
| Permission Directory | `governance_rule/permission_directory/` | permission-sovereign |
| Execution Layer | `governance_rule/execution/` | governance-authority |
| Core System | `main-system/src-core/core_system/` | system-sovereign |
| IPC | `main-system/src-core/ipc/` | runtime-sub-sovereign |
| Tasks | `main-system/src-core/tasks/` | maintenance-sovereign |
| Managers | `main-system/src-core/managers/` | resource-sub-sovereign |
| UI | `main-system/src-ui/` | interface-layer (P20) |
| Shared Layer | `shared-layer/` | integration-sub-sovereign |
| Local Model | `local-model/` | xingcheng |
| Independent Tools | `ai-assistant/`, `ai-collaboration/`, etc. | governed-executor |
| Scripts | `scripts/` | — |

### local-model (E:\GPTBridge-worktrees\local-model)

Local model platform development — Xingcheng core, model hub, model dialogue.

| Module | Path | Sovereign |
|---|---|---|
| Xingcheng Cognition | `local-model/xingcheng/cognition/` | xingcheng |
| Xingcheng Identity | `local-model/xingcheng/identity/` | xingcheng |
| Xingcheng Runtime | `local-model/xingcheng/runtime/` | xingcheng |
| Xingcheng Databases | `local-model/xingcheng/databases/` | data-sub-sovereign |
| Model Hub | `local-model/local-model-hub/` | xingcheng |
| Model Dialogue | `local-model/model-dialogue/` | xingcheng |
| Model Source | `local-model/src/` | xingcheng |
| Model Config | `local-model/config/` | xingcheng |
| Model Tests | `local-model/tests/` | language-review-sub-sovereign |

### rag (E:\GPTBridge-worktrees\rag)

RAG four-sub-architecture development (A52/E38).

| Module | Path | Sub-Architecture |
|---|---|---|
| RAG Core | `local-model/src/rag/` | shared |
| Hybrid RAG | `local-model/src/rag/hybrid.py` | hybrid-rag |
| Code RAG | `local-model/src/rag/code.py` | code-rag |
| Agentic RAG | `local-model/src/rag/agentic.py` | agentic-rag |
| Memory RAG | `local-model/src/rag/memory.py` | memory-rag |
| Qdrant Backend | `local-model/src/rag/qdrant_backend.py` | shared-index |
| RAG CLI | `local-model/src/rag_cli.py` | interface |
| RAG Tests | `local-model/tests/test_local_rag.py` | validation |

### ui (E:\GPTBridge-worktrees\ui)

Interface layer development (P20/A45/E31 — presentation only, no decide/exec).

| Module | Path | Language |
|---|---|---|
| Main Process | `main-system/src-ui/main/` | TypeScript |
| Renderer | `main-system/src-ui/renderer/` | TypeScript/React |
| Governance Bootstrap | `main-system/src-ui/main/governance-bootstrap.ts` | TypeScript |
| IPC Transport | `main-system/src-ui/main/ipc-transport.ts` | TypeScript |
| Path Library | `main-system/src-ui/main/pathLibrary.ts` | TypeScript |
| TS Checkers | `governance_rule/execution/typescript/` | TypeScript |

### git (E:\GPTBridge-worktrees\git)

Git governance tooling development (A53/E39).

| Module | Path | Function |
|---|---|---|
| Git Tier Gate | `governance_rule/execution/git_tiers/__init__.py` | three-tier classification + enforce |
| Git Gate Wrapper | `scripts/git-gate.py` | interactive git wrapper |
| Pre-push Hook | `.git/hooks/pre-push` | Tier-3 force-push blocking |
| Audit Ledger | `governance_rule/execution/audit/git_tier_audit.jsonl` | A46 audit records |

## Merge Flow

```
                Git Coordinator
                       |
        +--------------+--------------+
        |              |              |
      AI-1           AI-2           AI-3
        |              |              |
     worktree       worktree       worktree
        |              |              |
     branch A       branch B       branch C
        +--------------+--------------+
                       |
                  git-gate
                       |
                  git_tiers
                       |
               Local Bare Repo
                       |
                 Merge Queue
                       |
                     main
```

Each feature branch merges into `main` after verification.  The Git
Coordinator (`governance_rule/execution/git_tiers/coordinator.py`)
serializes merges through a merge queue so only one merge runs at a time.
Every operation is audited with pre-operation snapshot (HEAD, branch,
dirty files, staged files) for recovery.  `main` is the sole stable
release branch and the source of truth for the central bare repository
(`E:\GPTBridge.git`) and GitHub mirror (`origin`).

## Central + Mirror

| Remote | URL | Role |
|---|---|---|
| `central` | `E:\GPTBridge.git` | Local bare repo, central authority |
| `origin` | `github.com/guilty442-source/GPTBridge.git` | GitHub mirror |
