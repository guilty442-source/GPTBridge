# 自動檔案清理
Project ID: tool-mpz30cfk-hfnf

功能：
- 逐資料夾掃描並顯示目前進度。
- 可在工具視窗按「停止掃描」，保留目前已找到的結果。
- 找出內容完全相同的重複檔，使用 SHA-256 比對。
- 可選擇將重複副本移至應用程式專屬資料夾，保留每組第一個檔案。
- 可選擇性分析圖片，使用本機人臉偵測列出未偵測到人臉的無人像候選。
- AI 風景特徵辨識為選用模式；圖片分類只供檢查，不會列入批次刪除。
- 執行結果可直接「開啟」檔案或在檔案總管顯示「位置」方便檢查。
- 刪除採勾選方式執行，未勾選的檔案不會被刪除。
- 掃描報告輸出到應用程式專屬資料夾中的 `cleaner_report.json`。

預設資料位置：
- Windows：`%LOCALAPPDATA%\GPTBridge\duplicate-cleaner\...`
- 其他系統：`$XDG_DATA_HOME/GPTBridge/duplicate-cleaner/...` 或 `~/.gptbridge/duplicate-cleaner/...`

```powershell
.venv\Scripts\python.exe platform_tools/tool-mpz30cfk-hfnf/src/main.py <資料夾>
.venv\Scripts\python.exe platform_tools/tool-mpz30cfk-hfnf/src/main.py <資料夾> --json
.venv\Scripts\python.exe platform_tools/tool-mpz30cfk-hfnf/src/main.py <資料夾> --json --progress-jsonl
.venv\Scripts\python.exe platform_tools/tool-mpz30cfk-hfnf/src/main.py <資料夾> --auto-move
.venv\Scripts\python.exe platform_tools/tool-mpz30cfk-hfnf/src/main.py <資料夾> --image-analysis --allow-no-face-images --no-ai-landscape-features
.venv\Scripts\python.exe platform_tools/tool-mpz30cfk-hfnf/src/main.py <資料夾> --image-analysis --allow-no-face-images
.venv\Scripts\python.exe platform_tools/tool-mpz30cfk-hfnf/src/main.py <資料夾> --delete-selected-json '["path\\to\\file.jpg"]' --json
```
