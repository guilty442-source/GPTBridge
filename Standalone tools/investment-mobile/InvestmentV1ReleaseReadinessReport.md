# InvestmentV1ReleaseReadinessReport

- 工具：`investment-mobile`（投資管家 V1.0）
- 產出時間：本次驗收收斂工作完成時（worker: devin）
- 判定依據：`trading/acceptance.py` 的 `InvestmentAcceptanceMatrix` 實測結果
  ＋ `_stage_e2e_v1.py` 端對端腳本 ＋ pytest 全套 ＋ 治理稽核

---

## 1. 實際完成的功能

驗收矩陣實測 **31 PASS / 0 FAIL / 3 BLOCKED / 0 INCOMPLETE_EVIDENCE**。
PASS 項涵蓋（逐項含檔案位置、入口、測試證據，詳見矩陣輸出）：

- 台股持倉匯入與估值（`assets/`、`import` 管線，Decimal 計算）
- 美股持倉匯入與估值（含幣別換算 `exchange.py`，正確匯率口徑）
- 共同基金匯入與 NAV 估值（NAV 不當即時股價，已有測試鎖定）
- 全資產管理彙總（`total_assets` 契約，E2E 已驗證）
- 星澄 AI 整合（既有 `ChannelClient` 治理通道 `_ai_consult`；
  未綁定時安全降級，不建立第二套模型 Runtime）
- 投資建議產生（僅 advisory，不越過策略/風控/OMS 邊界）
- 策略引擎註冊、策略實驗建立
- 歷史回測（本地可重現資料）
- SHADOW 訊號（只記訊號、不產生成交，測試鎖定）
- PAPER 模擬交易（股票路徑成交、更新虛擬資產、績效計算）
- 風控（`risk_engine` + simulation `risk.py`，Decimal 口徑）
- 資料匯入（離線/手動帳戶與持倉）
- 投資報告產生
- 自動維護 / 啟動恢復（`startup_recovery`，事件去重、fill 數穩定）
- autotrade 狀態 PG 寫入器本身已實作並接線（見 §2 pg.persistence）

## 2. 實際未完成的功能（矩陣 BLOCKED，誠實保留）

| feature_id | 狀態 | 原因 |
|---|---|---|
| `pg.persistence` | BLOCKED | 寫入器已接線且可運作；`gptbridge_trading` schema 尚未套用（migrations 135–144 待治理遷移執行）。探針已改為自升級式：schema 落地後自動轉 PASS，不需改碼。 |
| `fund.paper` | BLOCKED | 共同基金 PAPER 的 NAV 結算路由未實作（基金模擬交易結算缺口）。 |
| `ui.control_route` | BLOCKED | 無已授權的入站控制路由；本工具 manifest 無自訂 UI，回 `CONTROL_CHANNEL_UNAVAILABLE`。 |

## 3. 本次修復的問題

1. `_stage_e2e_v1.py` `sys.path` 深度錯誤（`parents[1]`→`parents[0]`），
   原會 `ModuleNotFoundError`，整支腳本無法執行。
2. `_stage_e2e_v1.py` 與現行 API 漂移：估值回應 `total`→`total_assets`。
3. `_stage_e2e_v1.py` 恢復 API 漂移：`maintenance.recover()` 不存在，
   改接 `startup_recovery(engine)`，並驗證恢復後策略態、held-for-review、
   事件去重與 fill 數穩定。
4. `_stage_e2e_v1.py` 全域 `trading-mode-set PAPER` 與現行帳戶/Run 制不相容，
   改走現行 PAPER 帳戶路徑，模擬成交恢復產生。
5. `persistence_pg.py`（原為未追蹤半成品）：補上 migration-144 表格映射、
   schema/表存在性探測（`_schema_present`，60s 快取）、有界 spool
   （`pg_spool.jsonl`）、PG 缺席時安全降級不阻塞模擬主路徑。
6. `persistence_pg.py` 接入 `AutoTradingEngine`：`runtime.py`（run 註冊/
   狀態遷移/設定更新 → mirror_run + mirror_event）、`performance.py`
   （快照 → mirror_snapshot）、`maintenance.py`（啟動恢復時 best-effort
   `flush()`）。本地 JSON/JSONL 仍為 runtime 權威；PG 為輔助鏡像。
7. `acceptance.py` `pg.persistence` 由硬編碼 BLOCKED 改為證據驅動探針
   `_pg_mirror_probe`（未接線/schema 缺席→BLOCKED；schema 在→PASS）。

## 4. 本次修改檔案

- `Standalone tools/investment-mobile/_stage_e2e_v1.py`（commit `4e7cebf7`）
- `trading/autotrade/persistence_pg.py`（新增，commit `d5cd41ff`）
- `trading/autotrade/engine.py`、`runtime.py`、`performance.py`、
  `maintenance.py`（接線，commit `d5cd41ff`）
- `trading/acceptance.py`（探針，commit `d5cd41ff`）
- 本報告檔

## 5. 已移除的舊功能

無。盤點後未發現可證明無活躍依賴的舊頁面/舊 API/重複模組；
治理層、契約與歷史資料全數保留。

## 6. 完整測試結果

- pytest 全套（investment-mobile tests）：**338 passed in 8.05s**
- `_stage_e2e_v1.py` 端對端：**Ran 6 tests，OK**（涵蓋啟動→離線帳戶→
  台股/美股/基金匯入→全資產→行情→分析→建議→策略實驗→回測→SHADOW→
  PAPER→模擬成交→虛擬資產→績效→報告→重啟恢復）
- 驗收矩陣實測：31 PASS / 0 FAIL / 3 BLOCKED / 0 INCOMPLETE_EVIDENCE
- PG mirror 行為驗證：無 PG 環境下 run 註冊＋遷移共 3 次寫入全部 deferred、
  不產生 spool 檔、不影響主流程、status 誠實回報。

## 7. 離線安全驗證

- `LiveTradingCore(dispatch_enabled=False)`、`LiveActivationGate`、
  `BrokerGateway` 能力/API 驗證皆 fail-closed；LIVE 不可啟用。
- 目標工具內無 `requests/urllib/socket/websocket/aiohttp/httpx` 匯入；
  執行邊界沒有可用的正式券商送單路徑（非僅設定旗標）。
- SHADOW 不產生成交、PAPER 不改動手動匯入真實持倉（測試鎖定）。
- 無券商登入、無憑證取得、無真實委託送出。國泰/富邦複委託 OFFLINE。

## 8. 模擬操盤驗證

- PAPER 帳戶與手動持倉隔離；模擬成交更新虛擬資產並計算績效。
- 重啟恢復：策略態恢復、held-for-review 保留、事件去重、fill 數不翻倍。
- 無重複模擬成交、無跨帳戶資金污染（E2E + 單測覆蓋）。

## 9. 資料庫驗證

- migration `144_autotrading_center.sql`：simulated-only 協調表、
  `execution_mode IN ('SHADOW','PAPER')`、`simulated` 必為 true、
  AI actor 狀態遷移限制、RLS enabled+forced、schema `gptbridge_trading`。
- 歷史 migration 未被修改；無 mock 資料寫入權威投資表。
- 本地 runtime JSON/JSONL 為鏡像/快取定位正確；PG 寫入器已就緒，
  schema 套用為後續治理遷移步驟（非程式缺口）。

## 10. 效能與資源

- 全套測試 8.05s；E2E 腳本 0.41s；矩陣評估為一次性同步探針，無背景
  常駐成本。未觀察到重大記憶體洩漏跡象（短週期內無基線可量測，列 P3 觀察項）。

## 11. 尚存問題分級

- P0：無。
- P1：無阻礙主要離線使用流程者。
- P2：`fund.paper` 基金 PAPER NAV 結算路由未實作（部分功能缺失）；
  `pg.persistence` 待治理遷移套用 schema（寫入器已備妥、安全降級）。
- P3：`ui.control_route`（本工具無自訂 UI，屬設計內限制）；
  金融權威路徑已用 Decimal，殘餘 `float` 位於分析/統計/舊 OMS
  占位（`_daily_pnl` 明示回傳 0 的保守 stub）等非權威位置，
  建議後續版本逐一收斂；PG spool 檔在長期無 schema 環境下的
  清潔策略可再強化。

## 12. Release Candidate 狀態

完成條件核對：無 P0 ✔／無阻礙主流程 P1 ✔／核心 E2E 通過 ✔／
主要金融計算測試通過 ✔／離線安全通過 ✔／模擬操盤可恢復 ✔／
治理稽核 `audit_runtime_governance` 0 errors ✔。

**結論：`READY_FOR_RELEASE_REVIEW`**

（此為提交審查之就緒判定，非正式治理核准；三項 BLOCKED 已如實揭露，
其中 `fund.paper` 為最主要的後續補齊項。）
