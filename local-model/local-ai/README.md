# 星澄內部資料區

此目錄的固定系統標籤為 `local-ai`，隸屬於程式識別名稱
`local-model-platform`、中文顯示名稱「本地模型」的平台。

- `identity/`：獨立的 `local_ai_identity` 資料庫模組，依序建立 `role_data`、`role_history`、`role_audit` 與存取政策；初始化只建統一格式且資料數為 0，之後由模型對話補齊人格資料。
- `cognition/`：整合原生模型設定、模型角色、能力、業務認知與內部記憶為模型資料；舊有模型設定及角色資料均歸入此層。
- `databases/`：星澄可自治讀寫的兩個內部資料庫。
- `runtime/`：星澄執行期狀態。
- `permissions/`：唯讀權限快照；權限來源仍是 `governance_rule`。

星澄可以讀寫此資料區中除 `permissions/` 外的內部模型資料。對其他模組
只有全域唯讀存取與最高裁決權，沒有外部 SQL、檔案、程序或工具執行權。
