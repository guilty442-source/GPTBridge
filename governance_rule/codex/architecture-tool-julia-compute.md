# Julia 計算完整架構圖

```mermaid
flowchart TB
  CALLER[JuliaCompute Python 薄封裝] --> LOCATE[解析 julia 執行檔]
  LOCATE --> SPAWN[spawn-per-call]
  SPAWN --> STDIN[stdin 單一 JSON job]
  STDIN --> JULIA[Julia 計算程序]
  JULIA --> DESC[describe 統計描述]
  JULIA --> LS[least_squares 最小平方法]
  JULIA --> OPT[minimize 最佳化]
  JULIA --> MC[monte_carlo 模擬]
  DESC --> STDOUT[stdout 單一 JSON result]
  LS --> STDOUT
  OPT --> STDOUT
  MC --> STDOUT
  STDOUT --> CALLER
```

`julia-compute` 是獨立服務（`standalone-service`，按需啟動），法典 A610 指定的統計／科學計算與最佳化／模擬正式執行 owner。唯一受管入口是 Python 薄封裝（`main-system/src-core/core_system/julia_compute.py` → `JuliaCompute`）；Julia 執行檔解析順序為 `JULIA_EXE` 環境變數 → PATH → `%LOCALAPPDATA%\Programs\Julia-*` → `Program Files\Julia-*`（取最高版本），找不到即 fail-closed。

契約為 `julia-compute/v1`（`Standalone tools/julia-compute/CONTRACT.md`）：spawn-per-call，stdin 單一 JSON job → stdout 單一 JSON result；唯一外部依賴為 `JSON.jl`（其餘 Statistics／LinearAlgebra／Random 皆為 Julia stdlib，無網路需求），`Manifest.toml` 與 `.julia/` 為 gitignore 產物。所有失敗 fail-closed。

同步基線：A537、A538。
