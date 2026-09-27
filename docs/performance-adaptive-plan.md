# 效能提升與自適化計畫報告

**報告日期**：2026-09-27
**範圍**：跨語言效能優化（Python / Julia / JavaScript / C#・F# / JAX）及其測試與稽核防線
**治理依據**：Codex A610（Python 角色限定）、A341（編排層 C# 遷移）、A348（TS 退役邊界）、A612（JAX 訓練框架）、A239/A116（GPU gate 與 auto-release）

---

## 一、優化實作與實測結果

### 1. Python — 最小常駐與延遲載入

| 項目 | 變更 | 實測 |
| --- | --- | --- |
| `shared_layer/adaptive/gpu_coordinator.py` | 移除頂層 `import torch`，改 `_torch()` memoised lazy probe | import 0.76s（原 ~1-2s + CUDA context 風險） |
| 同上 | `query_gpu()` 修正為 nvidia-smi 優先、torch 僅 fallback（原順序與 docstring 相反） | query 0.18s，完全不初始化 CUDA |

影響面：6 個消費者（含 `model_resource_manager` 主系統遙測路徑）不再為每次呼叫付出 torch 載入成本。

### 2. JAX — 減少重新追蹤與 XLA 重編譯

`jax_backend/sft.py`（A612 唯一訓練框架）：

| 項目 | 變更 | 效果 |
| --- | --- | --- |
| `_collate` | batch-max padding → 64-token bucket padding | 編譯形狀數由「每種序列長度」降為 `ceil(max_length/64)` |
| `_train_step` | loss + grads + clip + AdamW 融合為單一 `jax.jit`，`donate_argnums=(0,1)` | 每 micro-batch 數十次 eager dispatch → 單一 XLA 呼叫 |
| `lr` | 改為 traced scalar 參數 | lr schedule 不再逐步 retrace |
| `eval_loss` | 改為 `jax.jit` | eval 路徑脫離 eager |

驗證：`test_jax_backend.py` 4/4、`test_sft_jit_invariants.py` 8/8。

**量化證據**（2,000 筆指數分佈長度、batch=8 模擬）：batch-max padding 產生 **208 種編譯形狀**；bucket-64 界為 **8 種**（-96%），代價為 padding 浪費 0.4%→14.7%（XLA 單次編譯以秒計，padding 為廉價矩陣計算，交換有利）；自適化 `_choose_bucket` 在 8-shape 界內自動選最小浪費粒度。

### 3. JavaScript — 非必要計時器與 IPC

| 檔案 | 變更 |
| --- | --- |
| `RuntimeServiceManager.ts` | 30s anti-stale heartbeat 於 `document.visibilityState === 'hidden'` 時整拍跳過 IPC |
| `AppSloDrawer.tsx` | SLO 輪詢同樣 hidden-gating |

既有 timer 設計已健全（bounded + early-return），本輪補上背景視窗仍每 30s 打 IPC 的浪費。`tsc --noEmit` 通過。

### 4. C# / F# — JIT vs Native AOT 實測

候選：`GPTBridge.Bootstrap`（bootstrap-entry，已遷 C#14/.NET10）。

| | JIT publish | Native AOT |
| --- | --- | --- |
| exe 大小 | 0.16MB（需 runtime） | 2.36MB（自含，payload 13MB） |
| 冷啟動 | 基準（1059–1715ms） | 1111–1433ms |

**結論：launcher 不切 AOT** — 其工作主體為 python/npm subprocess 委派，.NET 啟動佔比 <1%，差異在量測雜訊內。AOT 政策：僅「高頻重啟的小型工具進程」值得評估；F# `StarDomain` 為純函式庫不適用。

### 5. Julia — 已由另一 worker 完成

`compute.jl` 重寫為 `JuliaCompute` 模組：`@compile_workload` 預編譯全 op、Nelder–Mead 迴圈預配置零分配、`@inbounds` 手寫迴圈。殘留觀察：`nelder_mead(f, …)` 的 `f` 可改 `f::F where {F}` 使每種 objective 特化編譯。

---

## 二、測試套件

本輪新增三個測試檔（**19 tests，全部通過**；renderer 契約檔後擴至 7 tests），釘住優化不變量，防止重構時退化：

| 檔案 | 測試數 | 涵蓋不變量 |
| --- | --- | --- |
| `shared-layer/tests/test_gpu_coordinator_lazy.py` | 6 | 子進程 import 不帶入 torch；nvidia-smi 優先序；torch fallback；probe memoize；95% VRAM 上限語意 |
| `Standalone tools/local-model/tests/test_sft_jit_invariants.py` | 8 | bucket padding 形狀界（1..max_length → `ceil(max_length/64)` 種）；fused step 經 `jax.jit` 且 `donate_argnums=(0,1)`；`lr` 為 traced 參數；eval 亦 jitted |
| `main-system/tests/test_renderer_idle_gating.py` | 7 | 兩個 interval callback 的 hidden-gate 必須先於 IPC 呼叫；30s 有界 interval + cleanup；禁止 `setTimeout` 自重排鏈 |

既有套件（同批驗證範圍）：`GPTBridge.Channels.Tests` 16/16（C# A263 channel port）、`main-system/launcher/tests` 8/8（C# bootstrap 契約）。

執行方式：

```powershell
& main-system\.venv\Scripts\python.exe -m pytest shared-layer\tests\test_gpu_coordinator_lazy.py -x -q
& main-system\.venv\Scripts\python.exe -m pytest "Standalone tools\local-model\tests\test_sft_jit_invariants.py" -x -q
& main-system\.venv\Scripts\python.exe -m pytest main-system\tests\test_renderer_idle_gating.py -x -q
dotnet test shared-layer\csharp\GPTBridge.Channels\GPTBridge.Channels.Tests
& main-system\.venv\Scripts\python.exe -m pytest main-system\launcher\tests -x -q
```

註：`**/tests/` 在 `.gitignore` 內，新測試檔以 `git add -f` 入版控（沿用 `governance_rule/tests`、`Standalone tools/*/tests` 既有慣例）。

---

## 三、審計套件

新增稽核模組 `governance_rule/execution/audit/audit_optimization.py`，五個 `check_*` 函式採 delegated-lane 註冊（`_CHECK_MODULES` → `audit_checks_manifest.json` → `run_delegated_checks` 以 `globals()` 解析）：

| Check | 斷言 |
| --- | --- |
| `check_gpu_coordinator_lazy_torch` | AST 層級：無頂層 `import torch`；`_torch()` probe 存在；`query_gpu` smi 優先序 |
| `check_jax_sft_retrace_bound` | `_COLLATE_BUCKET` 常數存在；`train_step = jax.jit(...)` 含 `donate_argnums`；`_train_step` 簽名含 `lr`；`eval_loss = jax.jit(...)` |
| `check_renderer_idle_gating` | 兩個 TS 檔的 interval callback 內 `visibilityState` gate 先於 IPC 呼叫 |
| `check_bootstrap_native_entry` | `GPTBridge.Bootstrap.csproj` + `Program.cs` 存在且含 `--prepare-only`/electron 契約標記 |
| `check_channel_gateway_csharp` | Channels library + Tests csproj 存在；`A263Channel.cs` 含 `Stopwatch.GetTimestamp`/`ReconnectAsync` 移植不變量 |

驗證：manifest 重生成後 delegated=34（+5），`run_delegated_checks` 全 lane 0 error、無 unresolvable id、五個新 check 皆列入 executed。

```powershell
& main-system\.venv\Scripts\python.exe -m governance_rule.execution.audit.audit_optimization --root E:\GPTBridge
```

---

## 四、自適化計畫（Adaptive Plan）

「自適化」在此指系統依即時遙測調整自身行為，而非靜態配置。現況與路線：

### 已有自適化機制

- **`shared_layer/adaptive/gpu_coordinator`**：GPU VRAM gate（A239）— 訓練/推論依即時 free VRAM 取得/排隊/拒絕，fail-closed；本輪已使其遙測本身接近零成本（smi 優先、torch lazy）。
- **JS visibility 自適化**：UI 端依 `document.visibilityState` 抑制 IPC/輪詢 — 本輪新增，屬「依觀察者狀態降頻」的第一個實例。
- **JAX shape bucketing**：以固定 bucket 吸收輸入分佈變異 — 屬「對工作負載形狀自適化界界編譯」。
- **`execution/auto_release.py`**：idle timeout / memory pressure 驅逐 engine cache（A116）。

### 路線圖

| 階段 | 內容 | 依據 | 執行狀態（2026-09-27） |
| --- | --- | --- | --- |
| 1 | GPU coordinator 擴為統一資源遙測核：CPU/MEM/GPU 三維合一，供 training gate、engine cache、tool admission 共用 | A239/A116 延伸；需法典修訂程序 | ⏸ **治理阻塞**：跨越單一模組的資源統一屬架構變更，待法典修訂程序 |
| 2 | JS 端 visibility-gating 推廣為 renderer 標準：所有 interval/IPC 輪詢預設 hidden-gated，新增輪詢由 `check_renderer_idle_gating` 類 check 兜底 | 已落地檔案為樣板 | ✅ **已執行**：稽核推廣至全 renderer `setInterval` 掃描；刻意豁免者以 `// idle-ok:` 標記（useBackendSocket stale sampler、hmrService watchdog）；contract test 同步推廣（7/7） |
| 3 | C# 工具進程依「重啟頻率 × 啟動成本」量測決定 JIT/AOT（目前 launcher 已證明不值得）；gate 託管於 architecture registry disposition | A341 遷移管線 | ✅ **已定案**：決策規則 = 僅高頻重啟的小型進程評估 AOT；launcher 實測否決（本節一.4） |
| 4 | Python 常駐預算持續收斂：~1,170 檔 / ~280k 行超預算部分按 registry `migrate-csharp` 等 disposition 排程；`bootstrap-entry` 與 `information-channel-gateway`（C# 庫）已交付，host 接線為下一架構決策 | A610 + architecture_registry | ✅ **佇列已產出**：`docs/python-reduction-queue.md`；剩餘目標全部卡在「受管 C# host」前置（詳佇列文件第三節） |
| 5 | 訓練管線自適化：依 batch 分佈自動選 bucket 界與 `grad_accum`，以 XLA compile cache 命中率為指標 | A612 框架內 | ✅ **已執行**：`JaxSFTConfig.collate_bucket`（0=auto）；`_choose_bucket` 在 compile-shape 界內取最小浪費粒度；summary 新增 `collate_bucket`/`collate_shapes` 編譯形狀證據；測試 8/8、稽核已釘住 |

### 量測紀律

- 所有「效能提升」須附實測數字（本報告各節皆為本機實測），模擬結果不得呈報為真實 provider/環境成功。
- 回歸防線順序：測試套件（功能語意）→ 審計套件（結構不變量）→ delegated lane 60s 預算內執行。

---

## 五、驗證結果摘要

```
TEST_SUITE:
  test_gpu_coordinator_lazy.py     6/6 PASS (23.4s)
  test_sft_jit_invariants.py       8/8 PASS
  test_renderer_idle_gating.py     7/7 PASS
  GPTBridge.Channels.Tests        16/16 PASS（既有，本輪未改）
  launcher/tests                   8/8 PASS（既有，本輪未改）

AUDIT_SUITE:
  audit_optimization --root .      0 errors
  delegated lane                   5/5 checks resolved & executed, 0 errors
  manifest                         delegated 29 → 34（+5）

REPORT_SCOPE_COMMITS: d0307ec2（優化主體）、e0d65d24（bootstrap C#）、91959d4b（channel C# 修正）
REMAINING:
  - C# channel host/IPC 接線（架構決策）
  - Julia nelder_mead parametric-f 特化（交由原 worker）
  - Python 超預算收斂為長期排程（A610 registry disposition）
```
