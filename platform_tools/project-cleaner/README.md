# 清理工具

Project ID: `project-cleaner`

清理工具是獨立應用程式專案。主系統只保留應用程式入口與 IPC 橋接，清理規則、後端執行邏輯與清理工具視窗都放在本資料夾。

## 專案結構

- `manifest.json`：應用程式登錄資訊。
- `src/backend/cleanup_service.py`：清理規則與執行邏輯。
- `src/ui/ProjectCleanerWindowApp.tsx`：清理工具視窗。
- `src/ui/project-cleaner.css`：清理工具樣式。
- `src/main.py`：保留給獨立工具入口。
- `config/`、`assets/`、`logs/`、`build/`：工具專屬資料。

## 邊界規則

- 主系統不得放清理規則。
- 開發模式不得提供清理工具操作入口。
- 清理命令可透過主系統 IPC 橋轉送，但發起入口必須在 `ProjectCleanerWindowApp`。
