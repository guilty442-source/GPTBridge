# GPTBridge Architecture Boundaries

Target platform: Windows 11.

## Fixed Systems

- GPTBridge
- 介面系統
- 核心系統
- 設計模式
- 救援模式
- 開發者模式
- 設定
- 共用層

## Folder Boundaries

| System | Backend | Frontend | Responsibility |
| --- | --- | --- | --- |
| 介面系統 | `src-ui/renderer/info-center` | `src-ui/renderer/info-center` | Mode switching, system lights, lightweight status overview. |
| 核心系統 | `src-core`, `src-core/core_system` | `src-ui/renderer/core-system`, shared renderer helpers | Mother-tool core, shared layer, architecture rules, engines. |
| 設計模式 | `src-core/modes/design` | `src-ui/renderer/modes/design` | Child-tool project development only. |
| 救援模式 | `src-core/modes/rescue` | `src-ui/renderer/modes/rescue` | Mother-tool diagnosis, rescue, rollback preparation. |
| 開發者模式 | `src-core/modes/developer` | `src-ui/renderer/modes/developer` | Sandbox-only mother-tool development and deploy approval. |
| 設定 | `src-core/modes/settings` | `src-ui/renderer/modes/settings` | Governance, URL, account, storage, backup, cleanup, import/export. |

## Storage Boundaries

- Sandbox root: `.GPTBridge_RuntimeSandbox`
- Backup root: `backups`
- Design-mode backups: `backups/design-mode`
- Main-system backups: `backups/main-system`

Backups and sandbox must never be nested inside each other. Legacy backup folders are migrated into `backups/main-system`.

## Core Rules

- Core system and shared layer are governed together.
- Design mode must not modify GPTBridge mother-tool files.
- Rescue mode must not create or build child tools.
- Rescue mode is for saving the GPTBridge mother tool only; it must not run AI workflows or show AI answer panes.
- Settings must not edit code or deploy.
- Developer mode is the only mode allowed to modify the mother tool, and only through sandbox validation plus user approval.
