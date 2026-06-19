# [Level 2] Governance Enforcement Status

## 1. Enforcement Maturity Tracking (G-086)
每條規則必須有 `documented`、`advisory`、`partially enforced`、`fully enforced`、`runtime enforced`、`build enforced` 等成熟度標記。

## 2. Governance Coverage Minimum (G-087)
核心規則（Alias、Startup、Runtime、Imports、Circular Dependency、Runtime Core Duplication）最低必須達到 `partially enforced`。

## 3. Governance Regression Prevention (G-088)
任何治理覆蓋率降低（Governance Coverage Regression）必須是 `BLOCKING` 問題。