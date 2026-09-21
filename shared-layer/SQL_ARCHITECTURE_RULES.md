# SQL Architecture Rules v1（定版）

本檔是 GPTBridge 本地資料層的**正式定版規則**。規劃與分期整合於唯一共用藍圖
`Standalone tools/local-model/星澄模型四層建置藍圖.md`（第 6 章）；資料所有權見 `docs/DATA_OWNERSHIP_CONTRACT.md`；
本檔只寫「從此不再變動」的定位、authority、寫入、跨引擎、降級與禁止事項。

## 1. 引擎最終角色

| 引擎 | 最終角色 |
|------|----------|
| PostgreSQL | 中央結構化官方資料、共享傳輸、中央 Audit、Identity、RAG metadata |
| SQLite | 法典 authority（`governance_codex.sqlite3`，唯讀正式 authority）+ 模組私有狀態 + checkpoint + bounded fallback |
| Qdrant | canonical semantic/vector index |
| LOCAL-VECTOR | 若保留，只能 bounded non-canonical fallback |

`governance_codex.sqlite3` 是 SQLite 的特殊正式 authority，**不可被 PostgreSQL 取代**。

## 2. Authority 規則

- Structured shared truth → PostgreSQL
- Governance codex truth → `governance_codex.sqlite3`
- Semantic/vector truth → Qdrant
- Module-private operational truth → module-owned SQLite
- Cache / projection → **never authority**

衝突解決順序（**禁止 newest-timestamp-wins**）：

```
authority_class → generation → revision → formal decision
```

Timestamp 僅供輔助。實作：`shared_layer.workflow.resolve_conflict`。

## 3. 寫入原則

```
Identity → Permission → Governed Executor → Authority validation
→ Transaction/Saga → Audit → Verification
```

禁止 module 直接跨模組 SQL write；禁止模型自行產生並執行 SQL（模型只提供建議）。

## 4. 跨引擎規則

- 單一 PostgreSQL 操作：ACID transaction。
- PostgreSQL + SQLite + Qdrant + NTFS：durable operation + transactional
  outbox + idempotent Saga + verification + reconciliation。
- **禁止**把所有引擎偽裝成一個 transaction / 2PC / XA。
- PostgreSQL transaction 必須保持短：禁止在開啟的 PG 交易中等待 embedding、
  等待 Qdrant、大量檔案 I/O 或人工操作
  （`shared_layer.workflow.assert_short_transaction`）。

## 5. 降級規則

任何 fallback 必須同時 `bounded + observable + non-canonical + reconciled`，
缺一不可。SQLite fallback 必備上限：`max_pending / max_size / max_wal /
max_duration / reconcile deadline`；PostgreSQL 恢復後單向 SQLite → PostgreSQL reconcile。

## 6. RAG 邊界

```
Source Resource → PG resource metadata → PG chunk metadata → Embedding
→ Qdrant point → qdrant_point_id 回 PG → verification → READY
```

禁止 PostgreSQL 儲存 canonical vector、禁止 Qdrant 變成 structured authority。

## 7. 版本治理與 Startup/Shutdown

- Database Release 綁定：`release_id`、PG schema/migration head、role contract、
  RLS、query contract、SQLite template、reconcile contract、Qdrant contract、
  embedding contract、minimum runtime、certification result。
- 升級必經：Migration → Contract validation → RLS test → Compatibility test →
  Restore rehearsal → Certification → Activation。
- Startup ladder：`BOOTSTRAP → GOVERNANCE_VALIDATED → SECURITY_VALIDATED →
  DATABASE_FOUNDATION_READY → CENTRAL_AUTHORITY_READY → MODULE_PRIVATE_READY →
  SEMANTIC_INDEX_READY → RECOVERY_READY → READ_MODEL_READY → CORE_READY`
  （`shared_layer.startup_gate`）。正式寫入要求
  `authority_ready AND security_ready AND audit_ready`，`SELECT 1` 不算 ready。
- Shutdown：停止收新工作 → drain transport → 停背景 worker → checkpoint Saga →
  停 reconcile → flush audit/local state → 關 Qdrant → 關 SQLite → 關 PG pool。
  非 graceful shutdown 後的 startup 必須先做 unknown-commit check、lease
  recovery、SQLite WAL verification、Saga recovery、reconcile verification。

## 8. 健康與一致性字串（統一詞彙）

- Health：`HEALTHY / DEGRADED / UNAVAILABLE / DRIFTED / RECOVERING /
  QUARANTINED / UNKNOWN` + `reason_code`
  （`PG_POOL_EXHAUSTED`、`SQLITE_WAL_PRESSURE`、`SCHEMA_DRIFT`、`RLS_DRIFT`、
  `RECONCILE_BACKLOG`、`QDRANT_INDEX_LAG`、`BACKUP_STALE` …）。
  各模組不得自創健康字串（`shared_layer.health_states`）。
- Consistency：`CONSISTENT / PENDING / DEGRADED / RECONCILING / CONFLICT /
  ORPHANED / INVALID`（`shared_layer.workflow.evaluate_consistency`）。
  PG chunk READY + Qdrant point missing **不得**對外稱 READY。

## 9. 正式禁止事項

1. SQLite 作為共享中央官方 DB
2. SQLite 作為中央 shared audit
3. PostgreSQL 取代官方 `governance_codex.sqlite3`
4. PostgreSQL 取代 Qdrant canonical vector role
5. Qdrant 取代 PostgreSQL structured authority
6. LOCAL-VECTOR 升格成 canonical index
7. 模組自行跨模組直接寫 DB
8. 模組自行建立 identity / permission
9. Runtime 自行跑未知 migration
10. Repair Engine 自行修改 RLS
11. Repair Engine 自行 GRANT / REVOKE
12. 模型直接執行任意 SQL
13. Last-write-wins 解決 authority conflict
14. Timestamp 作為唯一版本判斷
15. Timeout 直接視為 transaction failed（必須 lookup + verify）
16. 跨引擎宣稱 exactly-once
17. PG transaction 中等待模型 / Qdrant / 大型 I/O
18. Cache / projection 成為不可重建資料來源
19. Audit 被 UPDATE / DELETE
20. Recovery 未驗證就解除 degraded state
21. Qdrant offline 就修改 PostgreSQL authority 迎合它
22. Schema drift 時偷偷自動 migration
23. RLS drift 時繼續正常寫入
24. Fallback 無上限累積
25. Backup 未 restore-test 就宣稱可恢復

## 10. Definition of Done（SQL v1）

- [x] PostgreSQL central authority
- [x] SQLite role boundaries（分類 A–D + ACL/path/scope）
- [x] governance codex read-only authority
- [x] Qdrant canonical semantic role（強制 module scope）
- [x] RLS deny-by-default + role layering（`security/roles.py` 認證）
- [x] central append-only audit（credential audit 只追加）
- [x] idempotent transport（idempotency_key + lease + reclaim，遷移 114）
- [x] bounded SQLite fallback（`security` / `workflow` 上限契約）
- [x] SQLite → PG reconciliation（單向）
- [x] formal RAG metadata authority（2026-09-21 核實：`PostgreSQLMetadataAuthority` 已由 `rag/pipeline.py:67` 實例化並接入 canonical_backend／generation／health_gate）
- [ ] startup certification 接上 runtime（`shared_layer.startup_gate` 十階梯已備；主啟動流程尚未映射階梯）
- [ ] backup restore certification 接上 scheduler
- [x] migration/release contract（checksum 鎖 + 必備清單）
- [x] cross-engine recovery test（`tests/test_workflow_consistency.py`）
