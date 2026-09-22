# 執行模組語言外移順序（P1 §1.1 細項① 收尾）

> 清單：`python-execution-modules-20260922.json`（19 模組：7 stay-Python＋12 migrate）
> 本文件補齊欠缺的「外移順序」；依雙軌制（§10.65：shadow→primary→retire，每元件 runtime flag、
> parity 測試、fail-closed 回 Python）逐模組執行，不得跳階段。
> 排序原則：①爆炸半徑由小到大（standalone → shared → in-process → gate-critical → entry）
> ②依賴閘控（E3/E4 未完成前不碰啟動面）③契約可驗證性（A263/logits/檔案語意有明確 parity
> 判據者優先於契約模糊者）。

| 階段 | 模組（清單 component_id） | 實效規模（含依賴閉包） | 目標語言 | 排序理由 |
| --- | --- | --- | --- | --- |
| M1 | `system-rescue` | 10 files / 581 LOC | C++ | 獨立工具、按需啟動、零常駐影響——最安全的雙軌制試點 |
| M1 | `investment-mobile` | 19 files / 706 LOC | C# | 同上；業務型工具適 C# 承接 |
| M2 | `information-channel-gateway`（channel_runtime + mixins 閉包） | 6 files / 1083 LOC | C++ | A263 契約明確（heartbeat/outbox/cursor/無重複副作用），parity 判據可完整列舉；但全通道共用、爆炸半徑大 → 列 M2 非 M1 |
| M2 | `tokenizer` | 207 LOC wrapper（實效＝jieba 分詞引擎重實作） | C++ | 契約模糊（jieba dict/HMM/DAG 語意）——先產出分詞語意規格＋parity 語料庫才准施工 |
| M3 | `xingcheng-auto-repair-module`（central_repair + repair_* 閉包） | 15 files / 3568 LOC | C# | 修復執行屬自動化核心語域；治理判定仍留 Python（action allowlist 不變） |
| M3 | `xingcheng-auto-learning-module` | 1 entry / 682 LOC | C# | 同上（self-learning 觸發與執行面） |
| M3 | `language-review` | 23 files / 3145 LOC | C# | sub-sovereign 內部執行面；待主宰邊界穩定後遷移 |
| M4 | `self-commit-service`＋`integration-plane`＋`recovery-plane`（git_tiers 共源） | 72 files / 20238 LOC（三者同目錄共源） | C++ | commit gate 關鍵路徑＋治理鄰接——風險最高之一，列 M4；三者共源須作為單一遷移單元處理，不得分批 |
| M5 | `bootstrap-entry` | 6 files / 1088 LOC | C# | 行程入口——待 §10.65 E3（啟動面）完成後才可 shadow |
| M5 | `boot-core` | 418 files / 86045 LOC | C/C++/C#（依單元制映射） | 最後階段；受 §10.65 E4／C Sovereign 成熟度閘控，且需先完成單元制物理搬移才有可映射的遷移單元 |

## 各階段放行條件

- **M1→M2**：至少一模組完成 shadow→primary 觀察窗（≥1 個 release 週期）且 parity 零差異，證明雙軌機械可用。
- **M2→M3**：channel-runtime 的 A263 parity 套件（heartbeat deadline／outbox 不丟未確認事件／cursor 收斂）全綠。
- **M3→M4**：修復鏈在 C# 宿主下 action allowlist 與 fail-closed 語意逐項比對通過。
- **M4→M5**：git_tiers 遷移後連續 10 次受管 commit gate 全綠且 ≤30 s（A537 不迴歸）。
- **M5 內部**：boot-core 依單元制 unit 為單位分批，每批對應 codex `architecture_activation_states` 受管更新。

## 不變式

- Python 路徑在 retire 前不刪（§10.65）；每模組單一能力單一職責（混責先拆）；
- 按需使用不常駐：遷移後模組須驗證載入／卸載＋資源釋放；
- 整體淨占用不得因遷移上升（Python −100 MB 不得換他處 +150 MB）。

## 補充（2026-09-22）：M1 實效閉包修正＋ABI 規格

- 盤點 `system-rescue` 實效閉包後發現：其自身僅 87 LOC shim，**真正執行面為
  `GovernedToolRuntime` 框架**（worker/claim/WS 閘門/HTTP/健康快照，~1173 LOC）
  ——M1 的施工標的因此是「受管工具執行面協定」而非該 shim。
- 前置規格已產出：`governed-tool-runtime-abi-v1.md`（`star-governed-tool-runtime-abi/v1`），
  凍結 §1–§6 線上語義＋12 項 shadow parity 判據；`issue_token` 屬治理認證面，
  **未核定前 M1 採模式 B**（C++ 工具體＋Python transport 代理，不觸 token ABI）。
- `investment-mobile`（M1 C#）同受此規格約束；兩者共用同一 governed runtime 協定。
