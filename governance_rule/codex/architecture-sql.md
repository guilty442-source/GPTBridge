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
  PROJ --> DB[(PostgreSQL Enforcement)]
  WRITE[Canonical Write] --> OUTBOX[Transactional Outbox]
  OUTBOX --> TRANSPORT[Transport]
  TRANSPORT --> INBOX[Inbox Dedup]
  INBOX --> EFFECT[Exactly-once Effect]
  SQ[(SQLite Buffer)] --> RECON[Revalidation and Reconciliation]
  RECON --> DB
  BACKUP[Backup WAL PITR] --> ACCEPT[Restore Acceptance]
  A --> GATE{All five PASS}
  PARITY --> GATE
  I --> GATE
  T --> GATE
  ACCEPT --> GATE
```

同步基線：A528、A537、A538；啟動 10 秒、強制測試套件 20 秒、獨立審計流程 30 秒，逾時 fail-closed。
