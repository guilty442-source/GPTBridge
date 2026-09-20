# 星澄 Phase 5 品質分析報告（chat-foundation 系列）

> 日期：2026-09-20　作者：自動化施工紀錄　依據：`星澄模型四層建置藍圖.md` §4.1 Phase 5
> 所有數字來自 `xingcheng/runtime/logs/maturity-*.json` 與 eval suite 實測，非估計值。

## 1. 候選版本總覽（82M dense，皆由 chat SFT 產生）

| 版本 | 資料 | L3 一般 ppl | L4 生成 | L5 對話指令 | dialogue-suite ppl | 結論 |
| --- | --- | --- | --- | --- | --- | --- |
| jobs/final.pt（v12 active）| 早期 SFT | 245 | pass | **0/4** | **53.9** | 現役；只會「改寫參考資料」分佈，不會 chat |
| chat-foundation-v1 | 100 條 chat | 548 | 3/4 | 2/4 | — | 學會 `<|eot|>` 與選項指令 |
| chat-foundation-v2 | 211 條 chat | 545 | 3/4 | 2/4 | 2082 | 首個可用 chat 基底 |
| chat-foundation-v3 | 281 條窄 drill | 1754 | pass | 2/4 | — | 災難性遺忘開始 |
| chat-foundation-v4 | 354 條窄 drill | **2180 ✗** | — | — | — | 完全遺忘，跌回 L2，作廢 |
| chat-foundation-v5 | v4 chat + 120k 字 replay | 326 | 4/4 | 1/4 | — | replay 保住一般能力但 chat 被稀釋 |
| **chat-foundation-v6** | 重平衡 chat + replay | **345** | **4/4** | **2/4** | **229** | **目前最佳候選** |

## 2. 方法論結論

1. **混合重放有效**：v5/v6 證明 chat+一般語料 1:3 token 比可消除災難性遺忘（v4 的 2180 → v6 的 345，甚至比 v2 的 545 更好）。
2. **窄分佈 drill 有害**：v3/v4 證明只加同構指令資料會摧毀一般語言能力——方向性錯誤，已停止。
3. **eval suite 基線語意重要**：v6 vs 直系前身 v2 = -89% ppl（改善）；vs v12 = +326%（表面回歸）。v12 的 53.9 是「該套件評測文本即其訓練分佈」的假象，不能作為 chat 品質證據。

## 3. L5 瓶頸定位（誠實判定）

L5 四探針：回合邊界 ✓、限定回答 ✓、**逐字複誦 ✗、多輪記憶 ✗**。

- v6 echo 探針回覆「小林」：輸出訓練分佈中的記憶值，而非複製上下文中的目標。
- v6 memory 探針回覆「QF-74」：代號**格式**正確、**值**錯誤——學會了表層分佈。
- 判定：82M 欠訓練基底缺乏穩定 induction/copy 機制。五輪資料迭代（含多樣化複製內容、去代號主導）均無法跨越 → **能力缺口，非資料配比問題**。

## 4. Phase 5 驗收狀態

| 驗收項 | 狀態 |
| --- | --- |
| maturity L5 ≥75% 探針 | **未達**（最佳 2/4） |
| lifecycle 註冊新權重 | 完成（v6 = weights v13 + eval report v3；因 vs v12 閘門未過，已回滾 active→v12） |
| eval suite (dialogue v1) | vs v2 PASS / vs v12 FAIL（見 §2.3） |
| 品質分析報告 | 本文件 |

**Phase 5 判定：不通過（L5 未達）。** 不得進入 Phase 6 驗收宣稱。

## 5. 後續路線（依 CP 值排序）

1. **xlarge（502M）基底**：預訓練中（eval ppl 68@200 → 43@300）；收斂後接 v7-mixed SFT 重測 L5。
   - ⚠️ **tokenizer 不相容**：xlarge 為 vocab 32000（82M 線為 8192），chat 資料可重用（文本層）但 checkpoint/權重線獨立。
   - 已做預實驗：xlarge@200 + 120 步 CPU SFT → L3 未過即停（chat 格式 120 步不足），證明需等完整收斂＋足量 SFT。
   - 接力管線已佈署：xlarge final.pt 落盤後自動跑 CPU 混合 SFT（400 步）+ 成熟度認證。
2. **接受 L4 上限**：v6 作為對話基礎正式啟用（L5=2/4 但嚴格優於 v12 的 0/4），待更大基底再衝 L5。

## 6. 附註

- `training_job_executor.run_job` 目前對完成的 job `activate=True` 無評估閘門——與「閘門未過不得啟用」原則有落差，建議後續將啟用權移交評估管線（本輪手動註冊亦已依此原則回滾）。
- 失敗候選 v1/v3/v4/v5 共 ~8GB 未被 lifecycle 引用，待使用者確認後清理；v2 保留為對照、v6 保留為最佳候選。
