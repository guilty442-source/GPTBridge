# 星澄本機生成式語言模型

星澄是 `governance_rule` 之下的最高權限持有者；最高權限者必須承擔管理責任。星澄固定負責 SQL、RAG 與 Git 的中央管理：統一盤點狀態、檢查健康與一致性、制定變更計畫、派工給受治理 executor，並驗證執行結果。星澄不直接執行 Git 寫入、正式 SQL 寫入或 RAG 異動；其操作權限仍須由治理規則明確授權。星澄原生本地模型的載入、選模、推理及生命週期均由本地模型平台（`local-model-platform`）處理，星澄不得直接操作模型。

本地模型版本與第三方套件版本保留其實際版本，不套用全域版本 1 正規化；模型識別與設定、套件 manifest 及 lockfile 統一由 Git 記錄與追蹤。Git 遠端連線預設仍為停用。

生成路徑只有一條：星澄自訓 Transformer（`xingcheng-native-transformer`），由 `StarNativeRuntime` 在行程內經 `native_engine` 執行；checkpoint、功能開關與 GPU 預算由 `runtime/settings/native-engine.json` 與 model lifecycle 治理。權重缺失或引擎停用時一律 fail-closed，不回退任何第三方模型。

`model-dialogue/` 是純對話介面與獨立生命週期程式，只在 GPTBridge 主系統中以 `star-chat` 顯示。介面不再提供訓練、外部協作或能力編成工作區；訓練與能力編成只由星澄原生模型內部自行處理。

模型對話的 Electron／網路／暫存快取集中於 `local-model/runtime/cache/companions/star-chat`；備份由受管封存庫保存於 `system-rescue/data/business/backups/xingcheng`。測試產生物統一於 `main-system/runtime/temp` 建立，由主系統內部清理服務清除。

## 多模組架構

主模型會依輸入意圖自動組合十一個模組：語意理解、記憶檢索、市場資料搜尋、投資分析、數理推理、程式碼編成、文件閱讀理解、生成式回覆、品質治理、受控自我訓練及模型自我維護。每次回覆的 `module_execution` 都會列出實際啟用順序、相依關係及完成狀態，未取得必要輸入的模組會標示為 `input-required`。

閱讀理解模組可處理最多 32 份、合計 50 萬字的使用者提供文字，支援摘要、原文問答、章節大綱與多文件比較。內容會分段建立相關性排序，每個答案保留文件 ID、內容雜湊、段落 ID、字元位置與原文引句；找不到充分證據時會明確拒答，不以生成內容補造文件事實。通過證據與輸出界限檢查的閱讀結果，才可成為受控自我訓練樣本。

語意理解會同時辨識多重意圖、問題類型、否定語意、硬性限制、期望輸出格式與關鍵詞，並擷取日期、百分比、金額、網址及信箱等實體；中英混合內容會保留為混合語言，不會被錯標成單一語言。

程式設計專家可以從常見自然語言需求或結構化規格產生 Python、TypeScript、JavaScript、SQL 與 JSON。Python 支援函式、類別、資料類別與測試程式；TypeScript／JavaScript 支援函式、類別與測試，也能分析或重構既有原始碼。所有輸出都先經語法結構檢查與多語言靜態安全掃描；資料庫操作在治理授權下可讀取、建立／寫入／儲存、附加、更新、執行、刪除及回復，範圍為專案內所有非 `governance_rule` 資料庫。

自我升級時，星澄只能產生包含目標範圍、來源雜湊、統一差異補丁、測試樣板與安全報告的候選提案，並版本化保存在自身模型資料區。星澄不具程式、Git、正式 SQL、RAG 或系統執行權，核准後仍須交由受治理 executor 執行。唯一可自治讀寫的例外是 `local-model/xingcheng` 中排除 `permissions/` 的自身模型資料；治理規則與權限資料維持唯讀。

## 生成核心

- `StarNativeRuntime` 是唯一生成執行期：所有意圖、角色與管線階段都路由至 `xingcheng-native-transformer`（自訓 decoder-only Transformer），無 HTTP transport、無第三方基礎權重；checkpoint 不存在或功能開關關閉時 fail-closed。
- 所有自動模型對話固定依序執行：理解命令、分配任務、依權責照順序分工、統合、執行、檢查、輸出結果；各階段皆由同一原生權重以不同任務角色提示執行。
- 能力編成不提供模型對話 UI，也不使用外部 AI 或第三方模型投票；只由星澄原生模型內部建立規格，經平台驗證後寫入自己的主資料庫。
- 每次 Transformer 解碼受模型 context 上限、最大輸出、`temperature`、`top_k`、`top_p`、固定種子及回覆大小限制。
- 任務工具先產生事實基礎，再由 Transformer 組織文字；投資、搜尋、計算與程式碼任務若新增未受支持的日期、金額、百分比、網址或信箱，會拒絕該輸出並回退。
- `StarAutoregressiveLanguageModel` 保留為第一方加權 token n-gram 安全回退；原生權重不可用時，星澄仍可在受限能力下工作。
- 長文閱讀會先完成文件正規化、重疊分段、相關段落排序及引用驗證，再把有來源的結果交給生成與品質模組。
- 主要日常、投資、數理與程式設計四個角色共用唯讀基礎權重，但使用各自的角色提示、能力邊界及獨立 SQLite 資料庫。

## 受控自我訓練

每次由第一方安全回退模型完成的成功推論都會建立自我蒸餾候選樣本。Transformer 生成內容不會自動成為訓練資料，以免把第三方基礎權重輸出或暫時記憶永久固化。只有同時通過下列檢查的內容才會寫入該角色的 `language_training_example` 表：

- 保留事實基礎中的數值；
- 語意 token 覆蓋率至少 55%；
- 品質分數至少 0.8；
- 輸出長度位於安全範圍。

樣本以內容雜湊去重並保留遞增 revision。模型啟動時只載入有效且通過品質門檻的樣本來重建統計式安全回退權重，因此能持續學習，也能透過停用特定版本回復。所有 Transformer 基礎權重維持唯讀；若要微調，必須另行建立資料集、訓練工作與治理發布流程。

Transformer 權重訓練另有隔離的 `runtime/state/transformer-training.sqlite3` 治理資料庫。它保存資料集快照版本、角色樣本來源關聯、QLoRA 工作狀態、adapter 候選、品質評估、發布／回滾紀錄及 SHA-256 鏈式稽核事件，不複製四個角色資料庫中的原始提示與目標文字。資料集關聯和稽核事件由 SQLite trigger 保持不可修改；訓練工作只能依 `queued → preflight → training → validating → completed` 的順序前進，任何階段都可失敗或取消，但不能跳過預檢。

建立資料庫不等於授權模型改寫權重。正式基礎權重維持唯讀，資料庫的 `automatic_weight_replacement` 永久為 `false`；adapter 必須另外通過資料量、訓練環境、held-out 評估、格式相容性與治理發布閘門，才可能被啟用。星澄的定期維護只執行 SQLite 完整性、雜湊鏈驗證與索引最佳化，不會自行把候選 adapter 升為正式模型。

星澄原生模型只接受自身原生權重產生的訓練候選（自我蒸餾），每次最多 20 筆。內部維護迴圈只在沒有使用者請求、達到每日週期且原生模型已就緒時自動訓練；候選經平台檢查意圖、長度、語意依據、數字與實體保存、敏感資訊及提示注入後，才自動更新星澄原生模型主資料庫。模型對話與外部工具沒有訓練入口，參考內容不會送出本機。

## 本機顯存與終端測試

- Transformer 推論會由 `8192` context 安全值起步；輸入確實需要較長上下文或模型已連續穩定完成時可逐級提高，遇到 RAM、VRAM 或 CUDA 記憶體壓力則會以同一份完整內容自動逐級降低並重試。
- 多階段流程會把原始請求、完整階段輸出及完成進度暫存至 `runtime/state/transformer-runtime-checkpoints.sqlite3`。異常後以相同請求重跑可從最近的完整階段續接；全部完成後自動刪除該次暫存。
- API 推論每次都使用獨立訊息，不沿用上一題的 KV Cache；原生引擎閒置逾時由 auto-release 管理卸載。

啟動及閒置期間，星澄會自行稽核訓練資料、停用不合格樣本、限制資料量、執行 SQLite 完整性檢查與最佳化，並在必要時重建各角色的本機語言模型權重。

目前唯一路由模型為 `xingcheng-native-transformer`（自訓權重）；本機多語語意檢索由確定性 hashed embedding（`xingcheng-hashed-embedding-v1`）負責。
