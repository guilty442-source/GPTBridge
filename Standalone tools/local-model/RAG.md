# 本地模型 RAG

本地模型與全專案共用同一向量庫，並建立模組隔離與資料定位：

- Embedding：`xingcheng-hashed-embedding-v1`（固定寬度 hashed n-gram，in-process）
- Vector DB：vectord-rs（Rust 向量引擎）`gptbridge_shared_knowledge`（強制 `module_id` 隔離）

中繼資料寫在 PostgreSQL 多 schema，保存資料定位、處理狀態、稽核、撤銷、
刪除、衝突解決與 vectord point 的對應。vectord 只負責向量檢索；原始文件仍保存在
各模組自己的 NTFS 計算磁碟區。完整稽核只使用 PostgreSQL 資料檢核，
不包含 SQLite fallback。

資料定位與 vectord point 的保存與回寫都是事務化，只保存資料層的 `locator_id`。
協調層的權限決策，只查 owning module；shared-layer 透過模組層的 `locator_map`
解析最終真實位置。

SQL、RAG、vectord 使用同一資源識別規範：

`{platform_id}:{module_id}:{data_category}:{resource_type}:{resource_id}`

本工具固定使用 `platform_id=local-model-platform`、`module_id=xingcheng`。
- Keyword：PostgreSQL Full Text Search
- Fusion：Reciprocal Rank Fusion（RRF）
- Reranker：`Qwen/Qwen3-Reranker-0.6B`，單次 Hugging Face 檢查與載入
- Router：`xingcheng-native-transformer`
- 一般 RAG：`xingcheng-native-transformer`
- 快速 RAG：`xingcheng-native-transformer`
- 深度 RAG：`xingcheng-native-transformer`
- 摘要與萃取：`xingcheng-native-transformer`
- 審計 RAG：關係型驗證（純關聯查詢與文件溯源，fail-closed）
- 完整性 fallback：`xingcheng-native-transformer`（無第三方資料覆寫）

vectord-rs 監聽 `127.0.0.1:8092`，資料存在其 `runtime` 儲存目錄。本地模型啟動時若未偵測到服務，會經由 `startup_core` 的 `vectord-start` phase 啟動 `Standalone tools/vectord-rs/bin/vectord.exe`。

## 操作

在 `E:\GPTBridge` 執行：

```powershell
python eval\rag_cli.py status
python eval\rag_cli.py index xingcheng\README.md xingcheng\RAG.md
python eval\rag_cli.py query "本地 RAG 使用哪個 embedding 引擎？"
python eval\rag_cli.py query "快速檢索這份資料庫的內容" --mode fast
python eval\rag_cli.py query "哪些資源可刪除" --retrieve-only
```

支援 TXT、Markdown、RST、CSV、TSV、JSON、JSONL、HTML、XML、YAML、TOML、主要程式碼與 DOCX。不得覆寫 `E:\GPTBridge`、稽核目標或 `governance_rule`。

主要命令為 `xingcheng_rag_status`、`xingcheng_rag_ingest`、`xingcheng_rag_query`。
