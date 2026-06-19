# Platform Tool Isolation Governance

## Rule ID
- `G-115`

## Purpose
- Keep the main system stable by isolating each platform application as its own executable program.
- The mother application is only the unified entry, launcher, status tracker, and manager.

## Scope
- Main core: `src-core/**`
- Electron main process: `src-ui/main/**`
- Platform-tool workspace: `platform_tools/**`

## Rule
1. Platform-application implementation code must live under `platform_tools/<tool-name>/`.
2. Each platform application must declare `runtime.entry` and `executable.path` in its `manifest.json`.
3. Each platform application must be packaged as a standalone EXE under its own project folder, normally `platform_tools/<tool-name>/dist/<tool-name>.exe`.
4. Main core and Electron main process may launch, stop, list, and monitor applications, but must not contain application-specific runtime logic.
5. Main renderer must not import platform-application UI components from `platform_tools/**`.
6. If an application needs runtime actions, define and execute them inside that platform-application folder.
7. `src-core/tasks/<tool-name>/` is forbidden. Each platform application must own its runtime code under its own project folder.

## Enforcement
- Governance checker: `governance/code/checkers/modules/ChildToolIsolationChecker.ts`
- Current level: `BLOCKING`

## Migration Note
- Platform-tool runtime code must be migrated out of `src-core` and into `platform_tools/<tool-name>`.
- Run `npm run package:tools` before release so the mother application can launch the standalone EXEs.
