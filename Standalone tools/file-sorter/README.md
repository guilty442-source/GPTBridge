# 自動化檔案管理 V2

Project ID: file-sorter

此工具模組集中存放於 `platform_tools/file-sorter/`。

- 目標資料夾的第一層子資料夾名稱會自動成為關鍵字。
- 可在工具視窗新增關鍵字，並從目標資料夾內既有的第一層子資料夾選擇分類目的地。
- 每個分類規則都受目前目標資料夾限制；不接受絕對路徑、多層相對路徑或目標外的資料夾。
- 手動整理固定先產生預覽；只有明確確認後才套用，來源若在預覽後改變會拒絕搬移。
- 自動分類預設關閉。啟用後由 GPTBridge 後端常駐服務執行，不依賴工具視窗持續開啟；檔案需在連續兩次背景觀察中維持相同大小與修改時間才會搬移。
- 未完成下載、仍被占用或尚未安靜達指定秒數的檔案會略過。
- 搬移採交易日誌、無覆寫發佈與 SHA-256 驗證，中斷後可恢復。
- 歷史紀錄可檢視，最後一筆已完成交易可安全復原；若原路徑已被占用或檔案已變更，復原會停止。
- 分類目的地不會被自動建立，必須是目標內已存在的第一層子資料夾。
- 規則會從舊設定一次性遷移到每個目標資料夾各自的 V2 profile；舊檔不會被覆寫。
- V2 狀態預設存於 `%LOCALAPPDATA%\GPTBridge\file-sorter`，可用 `FILE_SORTER_STATE_ROOT` 覆寫。
- 無法分類的檔案會保留原位、不移動；同名檔案會自動加流水號避免覆蓋。
- 媒體掃描以 SQLite 快取影片指紋，並以候選索引減少大型資料夾的兩兩比較。
- 完全重複檔與相似圖片偵測已移除，不再提供相關 UI、CLI 或獨立工具入口。

```powershell
.venv\Scripts\python.exe platform_tools/file-sorter/src/main.py <目標資料夾> --preview-json
.venv\Scripts\python.exe platform_tools/file-sorter/src/main.py <目標資料夾> --apply-plan <plan-id>
.venv\Scripts\python.exe platform_tools/file-sorter/src/main.py <目標資料夾> --history-json
.venv\Scripts\python.exe platform_tools/file-sorter/src/main.py <目標資料夾> --undo-last
.venv\Scripts\python.exe platform_tools/file-sorter/src/main.py <目標資料夾> --set-profile-enabled true
.venv\Scripts\python.exe platform_tools/file-sorter/src/main.py <目標資料夾> --add-keyword idol --folder 偶像
.venv\Scripts\python.exe platform_tools/file-sorter/src/main.py <目標資料夾> --upsert-keyword idol --folder 偶像
.venv\Scripts\python.exe platform_tools/file-sorter/src/main.py <目標資料夾> --update-keyword old --new-keyword new --folder 新分類
.venv\Scripts\python.exe platform_tools/file-sorter/src/main.py <目標資料夾> --list-source-files
.venv\Scripts\python.exe platform_tools/file-sorter/src/main.py <目標資料夾> --cleanup-scan --similar-video-analysis --json
```
