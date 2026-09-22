# 法典內部去重同批 Amendment 草案（§3.5 A）

> 對應 `codex_semantic_duplication_scan` r10：800 provisions / 15 cross-layer / 151 prohibition-shared / 2 template

## 候選

- **Boilerplate 共同禁止條**：A235-247（13 條）、A503-513（11 條）、A248-251+A536 → 候選「共同禁止條 + 各條 INHERITS 引用」（12+4+11 已驗證，`3.5A: dedup design drafted`）
- **模板群**：A303-310、A316/A317、A299/A300；A529/A530 已由 A532 取代
- **跨層重述**：E7~A28、E2~A5 等 9 組 → 改引用 + 領域差異
- **Git 五重述**：A163/A375/A245/E138/P86 → 以 A375 為單一控制條
- **形式規則模板**：42 條 `declared provision` + 6 條相同 evidence predicate（A334 掛 8 條）→ 產生端去重

## 收斂方式

- 同批 amendment：`common-prohibition` + `INHERITS` 引用，不刪歷史、不變效力，`codex_convergence_closure` 重建
- 法典 `codex_semantic_duplication_scan` 已由 `DUPSCAN@2026-09-16 UNKNOWN` → `DUPSCAN@2026-09-22T03:49:55Z SCANNED` 填充

## 下一步

- 依 `3.5A: dedup design drafted` 待 governor 用詞核定後發布，屆時重建 `runtime-rule-index` + `business_rule_delegation` 對照
