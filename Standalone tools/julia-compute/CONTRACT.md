# julia-compute/v1 — 受管 Julia 計算契約

`julia-compute` 是 codex A610 PYTHON-WORK-TRANSFER 指定的統計／科學計算
與最佳化／模擬能力的**唯一正式執行 owner**（按需分析——非常駐、
spawn-per-call、無持久狀態）。

不變式：
- Julia 不取代 JAX（模型訓練仍是 Python+JAX）。
- Julia 不解析不可信資料（解析由指定 Rust 原生安全元件負責）——
  本端點只吃受管 Python wrapper 產生的結構化 job 文件。
- 沒有第二套正式執行 owner：統計／科學計算與最佳化／模擬不回流 Python。

## 傳輸

- spawn-per-call：`julia --startup-file=no --history-file=no --project=<tool> src/compute.jl`
- 輸入：一個 JSON job 物件，stdin 或 `--job <path>`
- 輸出：stdout 最後一行為單一 JSON 結果物件
- 無網路、無常駐狀態；逾時與資源上限由 `tool-isolation-policy.json`
  的 `julia-compute` 條目與 Python wrapper 共同執行

## Job

```json
{"contract": "julia-compute/v1", "schema": 1,
 "op": "stats.describe", "params": {...}, "request_id": "optional"}
```

## Ops

### `stats.describe` `{values:[f64]}`

`{n, mean, std, var, min, p25, p50, p75, p95, p99, max}`

### `stats.quantiles` `{values:[f64], qs:[0..1]}`

`{quantiles: [{q, value}]}`

### `stats.correlation` `{x:[f64], y:[f64]}`

`{pearson, covariance}`（等長且 n≥2）

### `linalg.lstsq` `{a:[[f64]], b:[f64]}`

最小二乘 `a\b`：`{x, residual_norm, rank, rcond}`

### `optimize.nelder_mead` `{objective, x0:[f64], max_iter?, tol?, a?, b?, c?}`

無導數最佳化。objective 為封閉集合（不評估任意表達式，fail-closed）：
- `"sphere"`：`Σx²`
- `"rosenbrock"`：標準 Rosenbrock
- `"quadratic"`：`½ xᵀAx − bᵀx + c`（需提供 `a`；`b`/`c` 可省略）

回 `{x, f, objective, converged}`。

### `simulate.monte_carlo` `{model, trials?, seed?, ...}`

- `"normal"`：`{mu, sigma}` → 終端樣本矩與分位數
- `"gbm"`：`{s0, mu, sigma, t}` 幾何布朗運動終端值

回 `{model, trials, seed, mean, std, p05, p50, p95, min, max}`。
`trials` 上限 10⁷（fail-closed）。

## 錯誤

```json
{"ok": false, "error": "CONTRACT_MISMATCH | SCHEMA_VERSION_MISMATCH |
 INVALID_JOB_JSON | JOB_NOT_OBJECT | UNKNOWN_OP | OP_EXCEPTION:<type> |
 <op-specific code>"}
```

wrapper 端把 `ok:false`、非零退出碼、逾時、壞 JSON 一律轉為
`JuliaComputeError`——任何失敗都不會被 Python 靜默接手。
