# arch-next/codex_arch — Reference Architecture (Superseded)

## Status

**State: superseded-by-src-core**

The design patterns from this directory have been integrated into the
production decision layer at `src-core/core_system/codex_decision.py`.

## What Was Integrated

The following patterns from `codex_arch/` were extracted and integrated
into `src-core/core_system/codex_decision.py`:

| arch-next source | Integrated into | Pattern |
|---|---|---|
| `shared/basis.py` | `codex_decision.py` | `DecisionBasis`, `verified_basis()`, `is_provision_token()`, `provision_text()` |
| `shared/contracts.py` | `codex_decision.py` | `SovereignRequest`, `SovereignOutcome`, `Refusal`, `refusal_outcome()`, `accepted_outcome()` |

## What Was NOT Integrated

The following modules were **not** integrated because the production
`src-core/core_system/` already has working equivalents:

| arch-next source | Production equivalent | Reason |
|---|---|---|
| `shared/gate.py` | (sovereign entry methods) | Production sovereigns use direct method entry, not a gate pattern |
| `governance/delegation.py` | (governed executor delegation) | Production sovereigns delegate via app.governance and toolbox |
| `governance/authority.py` | (codex integrity) | Codex integrity is handled by the codex loader and governance audit |
| `sovereigns/_base.py` | (sovereign classes) | Production sovereigns derive identity from codex dynamically |
| `sovereigns/*.py` | `*_sovereign.py` / `*_sub_sovereign.py` | Production implementations are complete (14/14 sovereigns) |

## Why arch-next Is Incomplete

`arch-next/codex_arch/sovereigns/` only contains 8 of 14 codex sovereigns:
- system, runtime, maintenance, permission, data, resource, integration,
  permission_directory

Missing: startup, learning, programming, third-party, language-review,
governance-authority, xingcheng.

It also uses legacy sovereign IDs (e.g. `system-sovereign`,
`system-resource-sub-sovereign`) that have been superseded by the codex
sovereign IDs (e.g. `decision-sovereign`, `resource-sovereign`).

## Retention

This directory is retained as a **reference architecture** for the
gate-based and delegation-based design patterns. It is not imported by
any production code and should not be extended.
