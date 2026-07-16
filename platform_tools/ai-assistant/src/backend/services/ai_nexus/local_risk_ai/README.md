# 本地輔助AI

此資料夾是 AI投資管家的本地模式風險引擎。它可以由應用程式服務層呼叫，也可以單獨用 Python 執行。

預設會自動連網抓取公開報價資料，再由本地規則完成風險監測；不會呼叫外部 LLM。輸出中的 `data_mode` 會明確區分：

- `offline`：完全離線，只使用本機持倉與既有資料。
- `local_with_public_data`：分析仍在本機執行，但允許向公開市場資料來源取得報價。

報告生成時間、匯入時間與報價驗證時間皆使用本地設備時區。

## 單獨執行

```powershell
$env:PYTHONPATH = "platform_tools/ai-assistant/src/backend/services"
.venv\Scripts\python.exe -m ai_nexus.local_risk_ai --portfolio path\to\portfolio.xlsx
```

離線模式不抓報價，只檢查持倉資料、成本與集中度：

```powershell
$env:PYTHONPATH = "platform_tools/ai-assistant/src/backend/services"
.venv\Scripts\python.exe -m ai_nexus.local_risk_ai --portfolio path\to\portfolio.xlsx --offline
```

也可以直接下本地命令；命令沒有寫「離線」時會維持自動連網報價：

```powershell
.venv\Scripts\python.exe -m ai_nexus.local_risk_ai --portfolio path\to\portfolio.xlsx --instruction "連網 只看 AAPL 跌破成本 3% 集中度 30%"
```

此模組不呼叫外部 LLM；連網模式只使用既有報價 provider，並會輸出報價覆蓋率與健康狀態。

## 分數與資料可用性

預設分數的正式名稱是「啟發式風險代理分數」（`heuristic_risk_proxy`），只反映持倉、報價品質、集中度與本地警示，不是基本面評等、報酬預測或買賣訊號。

基本面、成長性、獲利能力、財務安全、籌碼與估值若沒有明確研究資料，會輸出：

- `status: unavailable`
- `score: null`
- `coverage: 0`
- `evidence_ids: []`

引擎不會用中性分填補缺漏。若要提供研究資料，必須使用 `research_inputs` 契約，附上可追溯欄位；沒有證據或沒有明確的 `normalized_score` 時仍不納入評分。

所有結果保留 `simulation_only: true` 與 `human_approval_required: true`，不會自動送出交易。

## 本地分析脈絡

`analyze_state` 可選讀 `state["analytics_context"]`，目前只接受 `risk`、`events`、`decision_journal`、`ledger_summary` 與 `policy`。內容會轉成數量受限的 `analytics:*` evidence IDs，供說明與反證條件引用；不會因此改變評分。其他 section、敏感憑證欄位、無關標的與過長內容會被忽略。

## 選用的本機語言模型

語言模型解釋層與此規則引擎分開。它只接受 loopback 上已安裝的 Ollama 模型，不會下載模型，也不允許遠端端點。輸出必須符合 JSON Schema、引用既有 evidence ID，且不得新增任何未被引用證據支持的數字；驗證失敗或資料不足時會回退到規則說明或安全拒答。

模型 profile 可設為 `compact`、`balanced`、`quality` 或 `auto`。`auto` 依本機 CPU 與記憶體採保守選擇；指定模型不存在時只會從已安裝模型中安全 fallback。

## 報價驗證規則

- 多來源價格差異超過 1% 時拒用該價格；超過 3% 時標記重大異常。
- 開盤期間報價超過 15 分鐘視為過期；收盤或快照資料超過 48 小時視為過期。
- 報價代號、持倉代號、幣別不一致時拒用該價格。
- 只有單一來源時可用但會標記為「單一來源」，不視為交叉驗證通過。
