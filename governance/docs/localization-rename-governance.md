# Localization Rename Governance

## Rule ID
- `G-I18N-001`

## Purpose
- Rename-sensitive UI labels must be controlled from localization files, so renaming does not require touching feature logic files.

## Scope
- Mode names (`資訊層`, `開發模式`, etc.)
- Mode page titles
- Toolbox name labels
- Platform display name
- Other product-facing labels that are expected to change by naming policy

## Source of Truth
- `src-ui/renderer/locales/zh-TW.ts`

## Enforcement
- Governance checker: `governance/code/checkers/modules/LocalizationRenameChecker.ts`
- Current level: `WARNING` (does not block build)

## Required Workflow
1. Rename by editing `src-ui/renderer/locales/zh-TW.ts` keys/values first.
2. UI code should reference localization values, not hardcoded Chinese labels.
3. Run `npm.cmd run governance:check` before build.

## Violation Pattern
- Hardcoded rename-sensitive labels in `src-ui/renderer/**/*.ts(x)` outside localization folders.
