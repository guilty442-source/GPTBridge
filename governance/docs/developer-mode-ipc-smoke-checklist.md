# Developer Mode IPC Smoke Checklist

## Goal
Verify the full command chain for developer-mode cards:
UI action -> IPC command -> backend result event.

## Automated (recommended)
1. Run `npm.cmd run smoke:developer`.
2. Confirm all checks are `PASS`.
3. If needed, run destructive cleanup test: `npm.cmd run smoke:developer:full`.

## Manual quick checks
1. Open 開發模式 > 工具執行層.
2. 系統設定:
- URL 欄位可載入/儲存。
- `檢查系統`有回傳結果。
- `開啟 ChatGPT/Gemini 瀏覽器`有回傳結果。
3. 清理工具:
- 執行清理後有回傳統計（檔案/資料夾數量）。
4. 沙箱工具:
- `沙箱維護`、`重新檢查健康度`皆有回傳。
5. 更新:
- `檢查熱更新`有回傳。
6. 備份:
- 建立備份記錄成功。
7. 日誌:
- 匯出操作紀錄成功，且卡片內有操作記錄。
8. 右下角 `立即重啟系統` 按鈕可按。

## Failure handling
1. If command returns `unhandled_command_result`, verify `src-core/ipc/handlers.py` routing.
2. If settings command fails, check `src-core/settings/service.py` implementation.
3. If websocket cannot connect, ensure backend server is running on `127.0.0.1:8765`.
