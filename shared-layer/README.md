# GPTBridge shared-layer

`shared-layer` 是程式化 PostgreSQL 中央 SQL、Resource Registry、Locator 與權限閘道。

星澄 Core／Orchestrator 統一管理本機模型平台：Git 保存程式、Schema、設定與開發歷史；PostgreSQL 保存結構化且可修改的正式資料；Qdrant 保存 RAG 語意向量索引；Ollama 執行本機理解、推理與操作。四者不得互相取代。星澄只有決策、統籌及唯讀管理權，不具系統執行、Git 寫入、正式 SQL 寫入或 RAG 異動權；實際操作只能交給受治理 executor。唯一例外是 `local-model/local-ai` 中排除 `permissions/` 的自身模型資料，可由星澄自治讀寫。Git 狀態與歷史只從本機 repository 讀取，不會自行連線 remote。
PostgreSQL 是真正的 SQL 引擎；Python 不模擬交易、約束、索引、鎖、RLS 或 Trigger。

## 邊界

- 模組實體資料仍由各模組的 `data/` 擁有。
- `registry.locations` 保存實體位置，但一般查詢只能讀取不含路徑的安全 View。
- 跨模組介面只接受 `resource_id`；實體位置只交給通過治理驗證的 Executor。
- Qdrant 位於 `local-model/runtime/qdrant`，命中結果必須回 PostgreSQL 驗證權限。
- 星澄角色只有全域讀取能力，沒有 SQL 寫入或直接執行能力。

## 全自動管理

設定 `GPTBRIDGE_POSTGRES_ADMIN_DSN` 後，由 Python 執行：

```powershell
python -m shared_layer.database.manager bootstrap
python -m shared_layer.database.manager health
python -m shared_layer.database.manager backup --file <backup-path>
python -m shared_layer.database.manager restore --file <backup-path>
```

本層不含安裝器，也不會下載 PostgreSQL、Qdrant 或模型。服務或本機 Runtime 不存在時，
Python 只會回報未就緒並停止啟動，不會擅自安裝軟體。

不需要 `psql`、pgAdmin、Docker 或人工初始化 SQL。Bootstrap 會以 maintenance
database 連線建立 Database/Role，再套用 Schema、Table、Index、RLS 與 Migration。
備份與復原由 Python 呼叫 PostgreSQL 官方 `pg_dump`/`pg_restore`，不經 shell。
