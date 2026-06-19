# 自動化檔案管理
Project ID: file-sorter

此工具模組集中存放於 `platform_tools/file-sorter/`。

- 目標資料夾的第一層子資料夾名稱會自動成為關鍵字。
- 可在工具視窗新增關鍵字，並指定要分類到的子資料夾。
- 也可指定已存在資料夾作為外部目的地，包含不同硬碟的資料夾。
- 工具視窗新增關鍵字時，如果關鍵字已存在，會自動更新目的地並接著整理檔案。
- 可勾選「開啟工具時自動執行分類」，下次打開自動整理目前目標資料夾。
- 可勾選「自動偵測新檔案並執行分類」，工具開啟期間會偵測根目錄新檔案並自動整理。
- 自動整理會沿用目前關鍵字規則，外部目的地可跨硬碟移動。
- 外部指定資料夾不會被自動建立，必須先存在。
- 自訂規則永久寫入工具設定 `src/keyword_rules.json`，由所有目標資料夾共用。
- 無法分類的檔案會保留原位、不移動；同名檔案會自動加流水號避免覆蓋。

```powershell
.venv\Scripts\python.exe platform_tools/file-sorter/src/main.py <目標資料夾>
.venv\Scripts\python.exe platform_tools/file-sorter/src/main.py <目標資料夾> --add-keyword idol --folder D:\分類\偶像
.venv\Scripts\python.exe platform_tools/file-sorter/src/main.py <目標資料夾> --upsert-keyword idol --folder E:\Archive\偶像
.venv\Scripts\python.exe platform_tools/file-sorter/src/main.py <目標資料夾> --update-keyword old --new-keyword new --folder E:\Archive\new
.venv\Scripts\python.exe platform_tools/file-sorter/src/main.py <目標資料夾> --list-source-files
```
