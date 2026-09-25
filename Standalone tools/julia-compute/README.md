# julia-compute

GPTBridge 受管 Julia 計算工具——codex A610 指定的統計／科學計算與
最佳化／模擬正式執行 owner（按需啟動，非常駐）。

## 供應（fresh checkout）

需要 Julia（現行驗證版本 1.13.0，codex 版本欄位「待正式鎖定」更新後
以法典為準）。安裝後解析唯一外部依賴 `JSON.jl`：

```powershell
julia --project="Standalone tools\julia-compute" "Standalone tools\julia-compute\_provision.jl"
```

其餘依賴（Statistics／LinearAlgebra／Random）皆為 Julia stdlib，無網路
需求。`Manifest.toml` 與 `.julia/` 為 gitignore 產物。

## 呼叫方

唯一受管入口是 Python 薄封裝：

```
main-system/src-core/core_system/julia_compute.py  →  JuliaCompute
```

```python
from core_system.julia_compute import JuliaCompute

jc = JuliaCompute()                     # locate julia, fail-closed if absent
jc.describe([1.0, 2.0, 3.0])            # stats.describe
jc.least_squares(a=[[1,0],[1,1]], b=[1,2])
jc.minimize("rosenbrock", x0=[0.0, 0.0])
jc.monte_carlo("gbm", trials=50_000, s0=100, mu=0.05, sigma=0.2, t=1)
```

Julia 執行檔解析順序：`JULIA_EXE` env → PATH →
`%LOCALAPPDATA%\Programs\Julia-*` → `Program Files\Julia-*`（取最高版本）。

## 契約

見 `CONTRACT.md`（`julia-compute/v1`）。spawn-per-call；stdin 單一 JSON
job → stdout 單一 JSON result；所有失敗 fail-closed。
