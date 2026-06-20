# 清理工具

Project ID: `project-cleaner`

清理工具負責 GPTBridge 專案內的暫存、快取、過期日誌與輸出檔。規則與執行邏輯保留在本工具內，主系統只負責啟動工具或接收結果。

## 功能

- 產生清理計畫：列出路徑、大小、原因、風險與略過項目。
- Dry-run 預覽：不改動檔案，只回傳預計處理項目。
- TTL 規則：近期日誌與備份類檔案不會立即被刪除。
- 隔離區：可先移入 `.GPTBridge_CleanerQuarantine`，預設保留 24 小時。
- 還原隔離批次：可依批次名稱把隔離項目移回原路徑。
- 安全邊界：跳過 Profile、依賴目錄、備份目錄、符號連結與 Windows reparse point。

## CLI

```powershell
python platform_tools/project-cleaner/src/main.py --cleanup-garbage --scope runtime --dry-run --json
python platform_tools/project-cleaner/src/main.py --cleanup-garbage --scope runtime --quarantine --json
python platform_tools/project-cleaner/src/main.py --purge-quarantine --json
python platform_tools/project-cleaner/src/main.py --restore-quarantine 20260625_120000 --json
```
