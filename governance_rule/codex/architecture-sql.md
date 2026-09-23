# GPTBridge SQL 架構圖

```mermaid
flowchart TB
  GOV[SQL Governance] --> A[Authority Closure]
  GOV --> S[Schema Migration Closure]
  GOV --> I[Information SQL Closure]
  GOV --> T[Transaction Concurrency Closure]
  GOV --> R[Recovery Closure]
  DIR[Object Directory] --> PARITY{Directory = Replay = Live}
  MIG[Migration Replay] --> PARITY
  LIVE[Live Introspection] --> PARITY
  PERM[Permission Decision] --> ART[Authorization Artifact]
  ART --> PROJ[Security Projection]
  PROJ --> DB[(PostgreSQL canonical enforcement)]
  SESS["Session Identity（GUC：actor／module／request／decision／correlation）"] --> DB
  GEN[Security Generation Fence] --> DB
  DSN["DSN 用途分離（runtime／reader／admin／backup）"] --> DB
  WRITE[Canonical Write] --> OUTBOX[Transactional Outbox]
  WF["Workflow Operation（leases／idempotent steps／補償表）"] --> OUTBOX
  WF --> COMP["REQUIRES_RECONCILE／QUARANTINED"]
  OUTBOX --> TRANSPORT["Transport（priority_class／deadline）"]
  TRANSPORT --> INBOX[Inbox Dedup]
  INBOX --> EFFECT[Idempotent Effect]
  PUB["Publish Barrier（PREPARING → INDEXING → VERIFYING → READY）"] --> DB
  PUB --> QD[(Qdrant scoped index only)]
  SQ[(SQLite owner-private／degraded fallback only)] --> RECON[Revalidation and Reconciliation]
  RECON --> DB
  ADAPT["Adaptive Envelope（pool 2–8／batch 50–500／breakers／cost gate）"] -. bounded control .-> DB
  ADAPT -. bounded control .-> TRANSPORT
  BACKUP[Backup WAL PITR] --> ACCEPT[Restore Acceptance]
  A --> GATE{Five closures PASS}
  PARITY --> GATE
  I --> GATE
  T --> GATE
  ACCEPT --> GATE
```

PostgreSQL 是唯一 canonical 中央正式狀態；Qdrant 僅存語意候選（`require_scope`，module_id 必填）。治理法典亦先建立完整 PostgreSQL schema、搬移資料並完成 schema／row／content／lineage／permission parity，原子切換 `governance-codex://official` 後，才刪除法典 SQLite 及其副本。跨引擎工作以 Saga 執行（PostgreSQL 為操作權威，migration `113_workflow_operation.sql`）：步驟冪等、可續跑、租約心跳；逾時以 lookup＋verify 判定，耗盡或未知即 `REQUIRES_RECONCILE`／`QUARANTINED`；發布屏障只有 `READY` 可讀，跨引擎結論退化為 `DEGRADED`／`CONFLICT`，不得假裝成功。身分、連線、憑證、工作階段、權限、輪替與撤銷由 Access Control Plane 承接（migration `087_security_identity_control.sql`）：工作階段識別以交易區域 GUC 綁定，憑證只存驗證摘要，敏感寫入遇過期 `gptbridge.security_generation` 一律 fail-closed。Transport 依 `priority_class`（critical／interactive／background／maintenance）與 FIFO 取用並跳過逾 `deadline_at` 請求（migration `058_transport_priority_queue.sql`）。Adaptive SQL Layer 只在 `AdaptiveEnvelope` 內調整（pool 2–8、batch 50–500、reconcile 1–2），無訊號時一律 ALLOW。業務規則由 owning module 以版本化契約自行管理，不進入治理法典，也不得成為平行規範權威。
