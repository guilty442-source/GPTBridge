# Gemini Code Assist Lockdown Governance

## Rule ID
- `G-SEC-002`
- Runtime rule id: `gemini_code_assist_lockdown`

## Purpose
- Enforce a hard block so Gemini Code Assist cannot directly write, delete, move, or modify project code/files.

## Scope
- Runtime governance operations through `src-core/governance/enforcer.py`
- Core write authorization through `src-core/managers/core_governance.py`

## Rule
1. Any actor identified as Gemini Code Assist is denied write actions.
2. The block applies even when action target is unspecified (fail closed for write actions).
3. Manual/user-authorized workflows must use a non-blocked actor identity and approved governance path.

## Actor Match Policy
- Exact aliases:
  - `gemini_code_assist`
  - `gemini-code-assist`
  - `gemini code assist`
  - `google_gemini_code_assist`
  - `google-gemini-code-assist`
  - `google gemini code assist`
- Token-based fallback: actor tokens containing `gemini` + `assist` + (`code` or `coder`)

## Enforcement
- Runtime blocking rule: `src-core/governance/rules/gemini_code_assist_lockdown.py`
- Core lock deny list: `src-core/managers/core_governance.py`
- Build checker: `governance/code/checkers/security/GeminiCodeAssistLockdownChecker.ts`
- Level: `BLOCKING` (forced)

## Required Workflow
1. Keep this rule enabled in `src-core/governance/rules_engine.py`.
2. Do not remove Gemini actor aliases from deny policy.
3. Run `npm.cmd run build:app` and `npm.cmd run smoke:developer` after governance changes.
