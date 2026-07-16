# 專案清理與系統救援工具

Project ID: `project-cleaner` · Version: `1.0.0`

這是獨立安裝與執行的專案維護工具，也是 GPTBridge 唯一的清理、異常修復與系統救援實作。

## 能力

- 全專案唯讀掃描與清理預覽
- 低風險異常自動修正
- 系統救援檢查與可復原修復
- 隔離、還原與 SHA-256 復原紀錄
- 儲存空間分析與保留政策

## 權限邊界

工具的變更權限僅限清單綁定的專案根目錄。以下目標禁止自動變更：

- 專案外路徑
- Git 追蹤的原始碼
- 相依套件與瀏覽器設定檔
- 使用中的發行檔與封裝鎖
- 無法確認為低風險的檔案

永久刪除只接受完整、未過期且低風險的預覽計畫。一般清理與修復優先使用隔離區，並保留可還原紀錄。

## CLI

```powershell
python platform_tools/project-cleaner/src/main.py --status --json
python platform_tools/project-cleaner/src/main.py --cleanup-garbage --scope global --dry-run --json
python platform_tools/project-cleaner/src/main.py --system-rescue-check --json
python platform_tools/project-cleaner/src/main.py --system-rescue-repair --json
```

封裝單一工具：

```powershell
npm run package:tool -- project-cleaner
```
