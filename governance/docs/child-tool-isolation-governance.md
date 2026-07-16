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
8. Mother-process source must not import implementation modules from `platform_tools/**`; discovery and process management must use manifests and the versioned runtime contract.
9. A platform EXE is stale only when its own manifest/version changes or when `config/tool-runtime-contract.json` leaves its supported compatibility range. Unrelated mother-tool source changes must not invalidate it.
10. Main startup must not instantiate platform services. A standalone process may instantiate only the service declared for its own tool id.
11. Packaging one tool must require an explicit tool id. Packaging every tool requires an explicit `--all` release operation.
12. Cleanup ownership belongs to `platform_tools/project-cleaner`. `run.py clean` may invoke it only through its command contract; `src-core` must not retain a second cleanup scanner.
13. The shared `src-core/utils/cleanup.py` module may provide recoverable quarantine primitives, but must not own cleanup policy or project scanning.
14. Project Cleaner mutation authority ends at `GPTBRIDGE_PROJECT_ROOT`. It must reject outside-project paths and links/reparse points.
15. Cleanup features are classified as read-only diagnosis, recoverable mutation, or package maintenance. Source edits, Git-tracked files, force unlock, process termination, active locks, current `dist`, rollback-incomplete data, dependency trees, browser profiles, and user data are forbidden.

## Enforcement
- Governance checker: `governance/code/checkers/modules/ChildToolIsolationChecker.ts`
- Current level: `BLOCKING`

## Migration Note
- Platform-tool runtime code must be migrated out of `src-core` and into `platform_tools/<tool-name>`.
- Run `npm run package:tools` before release so the mother application can launch the standalone EXEs.
