# GPTBridge Python / C++ 混合架構評估報告

**評估日期**：2026-09-04
**評估範圍**：GPTBridge 主系統、xingcheng、shared-layer、governance_rule
**動機**：整體對話流程從使用者輸入到模型回覆，全鏈路延遲過高

---

## 一、現狀摘要

| 層級 | 技術棧 | 核心功能 |
|------|--------|----------|
| UI | Electron + React + TypeScript | 介面渲染、IPC |
| IPC Server | Python WebSocket (port 8765) | 命令路由、工具生命週期 |
| Shared Layer | Python + PostgreSQL + WebSocket | 跨工具佇列通訊 |
| Local-AI | Python + Ollama HTTP loopback | 模型推理調度、管道編排、事實驗證 |
| Ollama | **Go 伺服器 + C/C++ llama.cpp 推理引擎** | 實際的 LLM 推理（已在 C++） |

**關鍵發現**：Ollama 的推理引擎已經是 C++（llama.cpp），Python 層只負責「協調」而非「矩陣運算」。因此 C++ 改寫的價值不在於重寫推理本身，而在於減少 Python 協調層的開銷。

---

## 二、瓶頸精確定位（按影響排序）

### 瓶頸 1：全域生成鎖 — 最大延遲來源
**位置**：`transformer_runtime.py:25-32`（`@_resource_preparation_lock`）
**問題**：`generate()` 方法被鎖序列化，同一時間只有一個推理能執行。即使系統有 4 個推論槽位（`MAX_CONCURRENT_TRANSFORMERS = 4`），Ollama HTTP 呼叫（2-180 秒）期間完全阻塞其他請求。
**C++ 化幫助**：❌ 無法改善（鎖是邏輯問題，不是語言效能問題）
**建議**：已在先前修正為 `_resource_lock`（僅鎖資源準備），可再進一步拆分

### 瓶頸 2：Worker 閒置輪詢退避 — 請求發現延遲
**位置**：`governed_runtime.py:234`
**問題**：Worker 閒置後退避最高 2.0 秒，新請求最長等 2 秒才被發現
**C++ 化幫助**：❌ 無法改善（輪詢策略問題）
**建議**：已降至 0.5 秒；可改用 PostgreSQL LISTEN/NOTIFY 通知機制

### 瓶頸 3：PostgreSQL 連線管理 — 每次操作新建連線
**位置**：`store.py:46-47`
**問題**：每個 DB 方法都呼叫 `psycopg.connect()`，無連線池。即使 `requirements.txt` 有 `psycopg[pool]`，程式未使用
**C++ 化幫助**：❌ 無法改善（架構問題）
**建議**：改用 `psycopg_pool.ConnectionPool`，連線建立成本從 ~5ms 降至 ~0ms

### 瓶頸 4：HTTP 序列化往返 — 每次推理的固定開銷
**位置**：`transformer_runtime.py:816-887`（`_http_chat_stream`）
**問題**：每次推理都要：JSON 編碼 → HTTP POST → JSON 解碼 → 逐行 json.loads 迴圈 → O(n²) 字串拼接
**C++ 化幫助**：⚠️ 部分改善（可用 llama-cpp-python 繞過 HTTP，但 Ollama 本身是 Go 伺服器，HTTP 開銷相對小）

### 瓶頸 5：事實驗證正則迴圈 — 每次推理的 CPU 密集點
**位置**：`transformer_runtime.py:1053-1061`（`_fact_values`）
**問題**：每次生成後對 prompt+output 各跑 5 個 `re.findall` + `re.sub`，長文本時 CPU 密集
**C++ 化幫助**：✅ 有幫助（C++ 正則比 Python 快 5-50 倍）

### 瓶頸 6：Embedding 餘弦相似度 — 純 Python 雙層迴圈
**位置**：`service.py:317-324`
**問題**：`math.sqrt(sum(v*v))` 逐元素純 Python 迴圈計算餘弦相似度
**C++ 化幫助**：✅ 高幫助（向量運算適合 SIMD 加速）

### 瓶頸 7：Token 逐字元估數 — 無意義的 CPU 消耗
**位置**：`transformer_runtime.py:929`
**問題**：`sum(1 for character in text)` 逐字元掃描計算 token 估計
**C++ 化幫助**：✅ 有幫助（但改用 `len(text) // 4` 估計更簡單）

---

## 三、三種改寫方案比較

### 方案 A：全面 C++ 擴展模組（pybind11）

**改寫範圍**：
- `transformer_runtime.py`（2639 行）→ C++ 擴展模組
- `service.py`（3797 行）→ C++ 擴展模組
- `governed_runtime.py`（515 行）→ C++ 擴展模組
- `store.py`（148 行）→ C++ 擴展模組

**優點**：
- 極致效能（正則、JSON、向量運算都可快 10-100 倍）
- 單一二進位分發

**缺點**：
- 工作量巨大（~7000 行 Python → C++）
- 需要 CMake/MSVC 編譯工具鏈
- 跨平台維護成本高（Windows/Linux/macOS 各需編譯）
- 每次 Python 版本升級都需重新編譯
- Python 的 asyncio/typing/generators 在 C++ 中難以對應
- PostgreSQL 驅動仍需 Python（psycopg 是 Python 套件）

**工作量**：6-12 個月（全職）
**風險**：🔴 極高（重寫等於重寫整個後端）

---

### 方案 B：最小化 C++ 熱點替換（pybind11）

**改寫範圍**：僅改寫效能瓶頸 #5、#6、#7

| C++ 模組 | 對應 Python | 功能 | 預期加速 |
|----------|-------------|------|----------|
| `fact_validator.cpp` | `_fact_values()` | 5 組正則事實驗證 | 10-50x |
| `vector_math.cpp` | `_embedding_retrieval` | 餘弦相似度計算 | 5-20x |
| `token_estimator.cpp` | `_estimated_token_count` | Token 估計 | 3-10x |

**Python 保留**：
- 整體調度邏輯（`generate()` 方法）
- HTTP 串流處理
- PostgreSQL 存取
- WebSocket 通訊
- 治理/權限層

**優點**：
- 工作量可控（~300-500 行 C++）
- 其餘 Python 程式碼不動
- 編譯簡單（單一 .cpp + pybind11）
- 效能提升集中在真正 CPU 密集的熱點

**缺點**：
- 仍需 CMake + MSVC 編譯環境
- Windows 上需安裝 Visual Studio Build Tools
- 每次部署需編譯或預編譯 wheel

**工作量**：2-4 週
**風險**：🟡 中等

---

### 方案 C：Python 優先（零 C++ 改動）

**改寫範圍**：純 Python 優化，不引入 C++

| 優化項目 | 改動位置 | 預期效果 |
|----------|----------|----------|
| PostgreSQL 連線池 | `store.py` | 每次 DB 操作省 3-5ms |
| 結構化 JSON（或用 orjson） | `transformer_runtime.py`, `store.py` | JSON 序列化快 2-5x |
| 正則快取/預編譯 | `transformer_runtime.py` | 已有部分快取，可再強化 |
| Token 估計改為 `len(text)//4` | `transformer_runtime.py:929` | 消除逐字元迴圈 |
| PostgreSQL LISTEN/NOTIFY | `governed_runtime.py` | 消除輪詢退避延遲 |
| HTTP 串流改用 `aiohttp` | `transformer_runtime.py` | 非阻塞 I/O |
| NumPy 餘弦相似度 | `service.py:317-324` | 向量運算快 5-10x |

**優點**：
- 零編譯、零工具鏈
- 維護成本最低
- 跨平台零額外工作
- 團隊可直接維護

**缺點**：
- 效能提升有限（正則/向量仍比 C++ 慢）
- Python GIL 限制多核利用

**工作量**：1-2 週
**風險**：🟢 低

---

## 四、推薦方案

### 短期（本週）：方案 C — Python 優先優化
以最小改動量獲得最大效益：

1. **`store.py` 連線池**（1 天）— 效益最高、風險最低
2. **`orjson` 替換 `json.dumps`**（半天）— JSON 序列化快 3-5x
3. **Token 估計簡化**（10 分鐘）— 消除無意義迴圈
4. **`NumPy` 餘弦相似度**（半天）— 向量運算加速
5. **PostgreSQL LISTEN/NOTIFY**（2-3 天）— 消除輪詢延遲

### 中期（2-4 週）：方案 B — 最小化 C++ 熱點
如果方案 C 效能仍不足，再針對事實驗證正則（`_fact_values`）和 embedding 相似度用 C++ 重寫。

### 長期（視需求）：不建議方案 A
全面 C++ 改寫的 ROI 太低。推理本身已經在 C++（Ollama/llama.cpp），Python 協調層的效能瓶頸可透過上述方案大幅改善。

---

## 五、C++ 改寫的真正價值點

如果決定做方案 B，以下是 C++ 化最有價值的三個模組：

| 優先序 | C++ 模組 | 功能 | Python 行數 | 預期加速 |
|--------|----------|------|------------|----------|
| 1 | `fact_validator` | 5 組正則事實驗證 | ~40 行 | 10-50x |
| 2 | `vector_similarity` | 餘弦相似度計算 | ~20 行 | 5-20x |
| 3 | `json_stream_parser` | Ollama 串流 JSON 解析 | ~60 行 | 3-10x |

**不建議 C++ 化的模組**：
- `governed_runtime.py` — 以 I/O 為主（WebSocket/PostgreSQL），C++ 無法改善
- `store.py` — 純 I/O（PostgreSQL），C++ 無法改善
- `service.py` 整體 — 3797 行中大部分是業務邏輯，CPU 密集部分很少

---

## 六、結論

**「整個流程都慢」的根因排序**：
1. 🔴 **全域生成鎖**（已修正為資源鎖）
2. 🔴 **Worker 輪詢退避**（已從 2.0s 降至 0.5s）
3. 🟡 **PostgreSQL 無連線池**（方案 C 1 天可修）
4. 🟡 **HTTP 序列化往返**（可改 aiohttp + orjson）
5. 🟢 **Python CPU 密集點**（正則/向量/JSON）

**建議路徑**：先完成方案 C（Python 優化），觀察改善幅度。若仍不足，再做方案 B（最小 C++ 熱點）。方案 A（全面 C++）不建議。
