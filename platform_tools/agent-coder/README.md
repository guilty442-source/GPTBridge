# 系統救援工具

Project ID: `agent-coder`

這是正式應用程式規則下的獨立專案資料夾。前端視窗程式碼位於：

`platform_tools/agent-coder/src/ui/`

此資料夾保存系統救援工具的 manifest、執行入口、前端視窗程式碼、設定、資產、紀錄與建置輸出，讓「應用程式」清單以標準 `platform_tools/<tool-id>/` 結構管理。

## 角色

- `manifest.json`：應用程式清單與入口定義。
- `src/main.py`：正式子專案入口與診斷輸出。
- `src/ui/`：系統救援工具的視窗 TSX/CSS。
- `config/`：預留系統救援工具設定。
- `assets/`：預留系統救援工具素材。
- `logs/`：預留救援紀錄。
- `build/`：預留建置輸出。

## 分離規則

- 系統救援工具只負責應用程式程式碼檢視、儲存、修補指令、強制介入與單元測試。
- AI 協作功能歸 `platform_tools/ai-assistant` 與 AI 協作工具視窗。
- 後端入口盡量使用英文輸出，避免命令列環境產生中文亂碼。
