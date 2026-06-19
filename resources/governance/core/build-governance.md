# [Level 1] Build Governance

## 1. Build Pipeline Governance (G-018)
Build pipeline 必須依序執行：
1. `governance:check`
2. `import check`
3. `circular dependency check`
4. `runtime validation`
5. `type-check`
6. `build`

若存在 `BLOCKING` 級別的治理問題，禁止建置。

## 2. Build Determinism
相同源碼必須得到相同的建置產物。禁止運行時修改建置行為。

## 3. Dependency Gatekeeping
新增依賴必須經過審核，包含理由、安全審查、維護審查、打包影響及治理相容性。禁止 AI 自動安裝 `npm install`。