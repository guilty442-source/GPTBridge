# [Level 1] Import Governance

## 1. Alias Authority (G-003)
`@` alias 唯一權威來源為 `src-ui/renderer`。

## 2. Alias Consistency
`vite.config.ts`、`tsconfig.json`、Python enforcer 及所有治理檢查器必須對齊此唯一權威。任何漂移皆為 `BLOCKING` 問題。

## 3. Deep Relative Import Prevention
禁止使用 `../../../../` 等深層相對路徑。所有內部引用必須使用 `@` 絕對路徑別名。