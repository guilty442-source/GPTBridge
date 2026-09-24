# 本機模組化 RAG

所有本地模型共用同一套知識庫，不建立模型專屬索引：

- Embedding：`xingcheng-hashed-embedding-v1`（確定性 hashed n-gram，in-process）
- Vector DB：Qdrant `gptbridge_shared_knowledge`（強制 `module_id` 篩選）

中央索引採 PostgreSQL 分層 schema，保存文件身分、模組歸屬、關聯、版本、
狀態、全文索引與 Qdrant point 對應。Qdrant 只負責向量；原始文件仍保存在
各模組自己的 NTFS 私有資料區。關鍵字索引只使用 PostgreSQL 全文檢索，
不提供 SQLite fallback。

中央索引與 Qdrant 不保存或回傳實體路徑，只保存不透明 `locator_id`。治理
授權成功後，才由 owning module／shared-layer 透過模組私有 `locator_map`
解析實際相對位置。

SQL、RAG、Qdrant 與稽核共用資源標籤：

`{platform_id}:{module_id}:{data_category}:{resource_type}:{resource_id}`

星澄固定使用 `platform_id=local-model-platform`、`module_id=xingcheng`。
- Keyword：PostgreSQL Full Text Search
- Fusion：Reciprocal Rank Fusion（RRF）
- Reranker：`Qwen/Qwen3-Reranker-0.6B`，只從 Hugging Face 本機快取載入
- Router：`xingcheng-native-transformer`
- 一般 RAG：`xingcheng-native-transformer`
- 快速 RAG：`xingcheng-native-transformer`
- 程式 RAG：`xingcheng-native-transformer`
- 深度推理：`xingcheng-native-transformer`
- 視覺 RAG：不支援（原生模型為純文字架構，fail-closed）
- 全域 fallback：`xingcheng-native-transformer`（無第三方備援）

Qdrant 僅監聽 `127.0.0.1:6333`，資料位於 `runtime/qdrant/storage`。本地模型啟動時若服務尚未執行，會自動啟動專案內的 Windows Qdrant 執行檔。

## 操作

在 `E:\GPTBridge` 執行：

```powershell
python xingcheng\src\rag_cli.py status
python xingcheng\src\rag_cli.py index xingcheng\README.md xingcheng\RAG.md
python xingcheng\src\rag_cli.py query "本地 RAG 使用哪個 embedding 模型？"
python xingcheng\src\rag_cli.py query "快速整理知識庫內容" --mode fast
python xingcheng\src\rag_cli.py query "找出相關段落" --retrieve-only
```

支援 TXT、Markdown、RST、CSV、TSV、JSON、JSONL、HTML、XML、YAML、TOML、常見程式碼及 DOCX。路徑只能位於 `E:\GPTBridge`，且明確禁止索引 `governance_rule`。

服務命令為 `xingcheng_rag_status`、`xingcheng_rag_ingest`、`xingcheng_rag_query`。

## Reranker 注意事項

Reranker 從 Hugging Face 本機快取載入官方 CrossEncoder 權重；權重尚未完整存在時，查詢會明確降級為 RRF hybrid ranking，不影響檢索與回答。
