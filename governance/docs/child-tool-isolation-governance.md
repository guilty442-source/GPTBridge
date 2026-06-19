# Platform Tool Isolation Governance

## Rule ID
- `G-115`

## Purpose
- Keep the main system stable by isolating platform-tool runtime logic from core runtime paths.

## Scope
- Main core: `src-core/**`
- Electron main process: `src-ui/main/**`
- Platform-tool workspace: `platform_tools/**`

## Rule
1. Platform-tool implementation code must live under `platform_tools/<tool-name>/`.
2. Main core and Electron main process must not hardcode any platform-tool ID or runtime logic.
3. Main renderer may keep only launch entries and localized labels for application display.
4. If a tool needs runtime actions, define and execute them inside that platform-tool folder.
5. `src-core/tasks/<tool-name>/` is forbidden. Each platform tool must own its runtime code under its own project folder.

## Enforcement
- Governance checker: `governance/code/checkers/modules/ChildToolIsolationChecker.ts`
- Current level: `BLOCKING`

## Migration Note
- Platform-tool runtime code must be migrated out of `src-core` and into `platform_tools/<tool-name>`.
