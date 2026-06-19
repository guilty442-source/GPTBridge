# Single Edge Browser Governance

- Rule ID: `G-BROWSER-001`
- Severity: `BLOCKING`
- Enforcement: `BUILD_ENFORCED`

## Objective

All browser-related features must use one shared Microsoft Edge core and one shared persistent profile.
Windows 11 is the required baseline runtime platform for this rule.

## Mandatory Constraints

1. Playwright provider context must use `channel="msedge"` only.
2. Playwright provider context must use one shared profile path:
   - `edge-profile/<profile>/shared`
3. System-browser auth flow must use the same shared profile path.
4. Browser launch flow must not force `--new-window`.
5. Channel installer must remain Edge-only:
   - `channels = ["msedge"]`
6. Startup behavior must remain stable on Windows 11:
   - `python src-core/main.py --help` must run successfully.
   - `npm.cmd run smoke:developer` must pass after killing port `8765`.

## Enforcement Points

- `src-core/managers/browser_session.py`
- `src-core/settings/service.py`
- `src-core/main.py`
- `governance/code/checkers/runtime/SingleEdgeBrowserChecker.ts`

## CI Gate

`npm.cmd run governance:check` must fail if any requirement above is violated.
