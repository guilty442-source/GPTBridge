# Windows 11 Stable Governance

- Rule ID: `G-W11-001`
- Severity: `BLOCKING`
- Enforcement: `BUILD_ENFORCED`

## Objective

The project must keep a stable Windows 11 runtime baseline and fail fast when baseline requirements are broken.

## Mandatory Constraints

1. Runtime config must pin platform baseline:
   - `target_platform = "Windows 11"`
2. Startup flow must keep Edge-first and stable serve entry:
   - `channels = ["msedge"]`
   - `await run_server(app_instance, profile=args.profile)`
3. Main runtime CLI must not expose `--headless`.
4. Project scripts must keep baseline validation commands:
   - `kill-backend-port` script for port cleanup
   - `smoke:developer` script for developer IPC smoke validation

## CI Gate (Mandatory)

`governance:check` must run in CI and block merge when any governance checker fails.

## Enforcement Points

- `src-core/settings/config.py`
- `src-core/main.py`
- `package.json`
- `governance/code/checkers/runtime/Windows11BaselineChecker.ts`
