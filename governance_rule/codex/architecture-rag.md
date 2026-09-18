# GPTBridge RAG 架構圖

```mermaid
flowchart LR
  CALLER[Caller] --> INFO[Information Channel]
  INFO --> AUTH[Permission and Scope]
  AUTH --> APP[RagApplicationService]
  APP --> DAG[DAG Planner and Executor]
  DAG --> CAG[CAG Gate: safe versioned scoped cache]
  CAG --> RAG[RAG Retrieval]
  RAG --> SUB[Hybrid / Code / Memory / Agentic]
  SUB --> QD[(Qdrant semantic index)]
  SUB --> PG[(PostgreSQL official metadata and FTS)]
  QD --> FUSE[Evidence Fusion]
  PG --> FUSE
  CAG --> FUSE
  FUSE --> RERANK[Local Reranker]
  RERANK --> CONTEXT[Context Builder]
  CONTEXT --> MODEL[Local LLM]
  MODEL --> CITE[Citation Validation]
  CITE --> RESULT[Result]
  SQLITE[(SQLite)] -. degraded fallback only .-> RAG
```

全專案採用 DAG、CAG、RAG 混合架構：DAG 是查詢、索引、修復工作流的編排面，不取代 Application Service；CAG 是安全、有版本、有範圍且非權威的加速與上下文重用面，不取代 RAG；RAG 是 canonical knowledge retrieval 面，保留 Hybrid、Code、Memory、Agentic 四子架構。Qdrant 與 PostgreSQL canonical 邊界不變，SQLite 只准 degraded fallback。同步基線：A528、A537、A538。
