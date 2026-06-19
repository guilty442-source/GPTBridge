# Governance Rules Interface Governance

## Rule ID

`G-UI-GOV-001`

## Purpose

治理規則管理必須是獨立介面，不得混回開發工具面板；使用者可輸入中文，系統會轉成規則代碼保存。

## Requirements

1. Renderer module must live in `src-ui/renderer/ui/governance-rules/`.
2. Main UI must expose `治理規則管理` as its own view.
3. All governance rules are active and global by default.
4. Chinese input must be converted into a stable rule code before saving.
5. Legacy `GovernanceRulesCard` must not remain in developer tool runtime panel.

## Enforcement

`npm run check:ui` runs `G-UI-GOV-001` and blocks drift.
