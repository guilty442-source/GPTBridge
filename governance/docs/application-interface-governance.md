# Application Interface Governance

## Rule ID
- `G-UI-APP-001`

## Purpose
- Keep the new application interface from drifting back to the old toolbox wording or legacy renderer entry.

## Rule
1. User-facing renderer text must call the application area `應用程式`.
2. User-facing renderer source must not display `工具箱`.
3. The renderer entry must load `src-ui/renderer/ui/App.tsx`.
4. The default view must open the application area.
5. The application entry must render cards through the developer-mode execution layer (`RuntimeToolCard` styles and behavior).
6. Internal technical names such as `toolbox_*` IPC commands may remain until a safe API migration is planned.

## Enforcement
- Checker: `governance/code/checkers/ui/ApplicationInterfaceChecker.ts`
- Category: `ui`
- Enforcement: `BLOCKING`
