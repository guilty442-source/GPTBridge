# ragd — Contract `ragd/v1`

Governed Rust RAG orchestration service (A610 PYTHON-WORK-TRANSFER:
RAG→rust). Loopback-only HTTP/JSON; refuses non-loopback binds.

## Authority boundary

ragd owns **retrieval orchestration only**. xstore canonical event replay is the sole
canonical authority: every vector candidate returned by vectord is
re-proved against `gptbridge_rag.chunk` ⨝ `gptbridge_index.resource`
(`index_status NOT IN tombstoned/deleted/purged` ∧ `NOT EXISTS
gptbridge_rag.tombstone purged=false`) and `gptbridge_rag.index_state`
(`status IN indexed, active`), then filtered by governed module scope and
the ACTIVE generation — a direct port of
`core_system/rag/pipeline.py::_apply_read_barrier`.

Fail-closed: unreachable vectord → `CANDIDATE_FETCH_FAILED`; unreachable
or invalid/unmigrated xstore → zero hits with `authority_error` set (a hit is
never returned unproved). An empty `module_ids` scope is rejected.

## Routes

- `GET /healthz` → `{ok, contract, service:"ragd", version, vectord,
  metadata_authority, metadata_store_configured, uptime_s, cag}`
- `POST /v1/rag/query` —
  `{collection, module_ids[], text|vector, top_k?, alias?,
  generation_id?}` → `{ok, contract, hits[], candidates,
  generation_id, dropped{unauthorized, generation_mismatch,
  missing_metadata, tombstoned}, authority_error}`
- `POST /v1/retrieve` — the full governed retrieval chain (A549):
  `{query, module_ids[], collection?, rag_types?=["hybrid"], kind?,
  session_id?, task_instruction?, top_k?, candidate_limit?,
  max_context_chars?, memory_scopes?, required_aspects?,
  permission_scope?, data_categories?, identity_id?, tenant_id?,
  data_classification?, generation_id?, budgets?, use_cache?,
  cache_level?, <CAG key dimensions>?}`
  → `{ok, contract, context_text, citations[], evidence[],
  evidence_count, sufficiency, generation_id, dag, cached}`.
  `kind` accepts `retrieval-chain` (default), `query`, `multi-rag`;
  `query`/`multi-rag` add the CAG cache nodes and still end at
  CONTEXT_BUILD — the MODEL_INFERENCE node has no `generate`
  callable and fails closed by design (ragd never fabricates
  answer_text). Side-effecting kinds (`index`/`rebuild`/`repair`)
  are refused.

## DAG plane (dag.rs)

Native port of `core_system/rag/dag/` (A549): the fixed node catalog
(12 types), closed state set, six bounded kinds, the declarative
planner with fail-closed validation (duplicate ids, missing required
inputs, dangling/self/unauthorized edges, acyclic, kind ordering,
budget envelope `≤64 steps / ≤600s / ≤10000 / ≤3 rounds`,
required node types), and the executor: deterministic topological
order, per-node timeout (30 s default, detached worker), bounded
retries, evidence-completeness checks, compensation records and a
sha256 evidence digest.

## CAG plane (cag.rs)

Native port of `core_system/rag/cag/`: bounded L1-L3 LRU caches
(256/1024/4096 entries, TTL 60/600/3600 s) behind the 23-check CAG
Gate — scope, permission, tenant, identity, classification,
normalized query, model/policy/revision/generation/embedding/
reranker/chunk-policy/context-builder versions, expiry, authority,
state and payload-digest integrity. A validated hit returns the
stored payload without running the DAG; a denied entry is evicted,
never served. Caches are derived/cache authority only, never
canonical.

## Retrieval lanes (retrieve.rs, lanes.rs)

Retrieval-plane roles (C106 / `data-architecture-contract.json`
`retrieval_plane_roles`): DAG is the sole global orchestration plane,
CAG the scoped non-authoritative cache, RAG the canonical retrieval
plane. The lanes below are governed data paths inside that DAG —
none is an independent orchestration or authority plane.

- `hybrid`     : vectord dense (barrier-proved) + native lexical
  retrieval (`index_state` proof) → `channel_fusion_hybrid` RRF
- `code`       : dense + FTS → RRF + symbol boost → CODE_SNIPPET
- `memory`     : dense + FTS → RRF → memory-scope/session filter +
  session boost → `memory_score` composite
- `agentic`    : single-agent autonomous retrieval — bounded
  retrieve→sufficiency→reformulate rounds over the hybrid core
  (`budgets.max_rounds`, hard cap 3)
- `multi-agent`: collaborative retrieval — the specialized peer lanes
  answer the same query concurrently and merge under the same bounded
  DAG (accepts `multi-agent`, `multiagent`, `multi-agent-rag`)
- `graph`      : specialized data path — relation-aware rerank over the
  in-pool evidence graph (shared-resource + sequence-adjacency +
  metadata-declared links; accepts `graph`, `graphrag`, `graph-rag`)
- `tag`        : specialized data path — structured/table canonical
  content under structured-authority (accepts `tag`, `tag-rag`)
- `multimodal` : specialized data path — media-bearing sources
  (declared modality or media-file source; accepts `multimodal`,
  `multimodal-rag`)

XRAG is **not** a lane: `compression: "xrag"` (or `"xrag"` inside
`rag_types`) runs extractive context compression inside CONTEXT_BUILD
after governed evidence retrieval (`context::compress_evidence`) —
verbatim sentence selection under the budget, never independent
retrieval, authority or cache. GAG is not a separate architecture and
resolves to the DAG plane; it never parses as a lane.

Fusion/rerank/context (`fusion.rs`/`context.rs`): architecture fusion
with authority-rank bonus, conflict flagging, no-external-reranker
passthrough (model `none`), and the fixed four-layer context builder
(governance / canonical / specialized / task instruction) with
adjacent-chunk merge and `[R#]` citation emission; citation
validation is fail-closed.

`generation_id` omitted → resolved from
`gptbridge_rag.generation WHERE state='ACTIVE'` for `alias`
(default `gptbridge_rag`).

## Bounded concurrency

Fixed worker pool + bounded pending queue (bounded-concurrency/v1,
same envelope as vectord): worker count is the governor `rag`-class
quota (`concurrency-budget/v1` from `GPTBRIDGE_GOVERNOR_STATE` or
`main-system/runtime/state/resource-governor.json`), clamped to
[2, 16], failing open to `available_parallelism`. Pending capacity 64;
a full queue rejects with `503 CAPACITY_EXHAUSTED`; a connection still
queued after 2000 ms is dropped unserved. `GET /healthz` reports live
counters under `conn` (submitted/completed/rejected/expired,
pending_approx). Never a thread per connection (PERF-04/PERF-05).

## Configuration

- `--bind` (default `127.0.0.1:8094`)
- `--vectord` (default `http://127.0.0.1:8092`, env `VECTORD_URL`)
- `--metadata-store` (else env `GPTBRIDGE_RAG_METADATA_STORE`), explicit migrated xstore path.

## Registry

`architecture_registry.json` component `ragd`
(execution/standalone-service, on-demand, non-canonical — orchestration
holds no formal authority). Binary: `bin/ragd.exe` (build artifact,
gitignored; `cargo build --release`).

## PostgreSQL retirement / native authority

Production ragd has no PostgreSQL driver, DSN resolver or credential access.
It links the existing Rust xstore event engine; no new database service is introduced.
Set `GPTBRIDGE_RAG_METADATA_STORE` or pass `--metadata-store <directory>`.
Legacy `--dsn` is rejected explicitly. Missing migration evidence fails closed,
including CAG cache hits and explicit generation requests.

Explicit source migration:

```powershell
ragd.exe --metadata-store <directory> --migrate-rag-source <export.json>
```

The source JSON contains all five arrays: `rag_generation`, `rag_chunk`,
`rag_resource`, `rag_tombstone`, `rag_index_state`. Every row has a nonempty
unique `record_id`; field names otherwise preserve the source table columns.
Chunks include `vector_point_id`, `chunk_id`, `resource_id`, `module_id`,
`sequence`, `character_start`, `character_end`, and `metadata`. Resources
include `resource_id`, `index_status`, and `metadata`. All source rows are
compared with independent target event replay before an append-only migration
receipt is written. Conflicting existing rows are rejected; matching partial
imports may resume. Timestamps added by the event engine are excluded from
source content parity. This receipt proves parity with the supplied export,
not independent verification that the export exhausts a live database.

This slice admits read-only migrated RAG metadata. Subsequent RAG row changes
invalidate the migration digest and fail closed pending a governed writer /
new baseline protocol; do not mutate live canonical rows behind this gate.
Native sparse ranking uses conjunctive exact Unicode alphanumeric terms and
frequency divided by token count, with stable chunk-id ties. It replaces
PostgreSQL `ts_rank`; ranking parity is not claimed. Vector candidates still
pass resource, tombstone, scope, generation and index-state barriers.
## Native SQL in process

Generation, chunk-candidate and index-state reads use the existing xstore
Rust library's bounded SQL evaluator over the verified canonical snapshot.
Supported grammar is `SELECT <registered columns or *> FROM <registered RAG
 table> [WHERE column = $n or column = ANY($n), joined with AND] [ORDER BY
 column ASC|DESC] [LIMIT $n]`. Parameters are separate JSON values; SQL literals,
joins, DDL, DML and transaction statements are rejected. NULL equality never
matches. Limits: statement 4096 bytes, 64 parameters, ANY arrays 4096 values,
10,000 matching rows. These limits never silently suppress authoritative rows.

The same engine exposes `xstore_sql_query_json` / `xstore_sql_buffer_free` in
`xstore.dll` for C/C++/C#/F#/Go/Julia/AOT callers. ABI declaration is
`xingcheng/src/backend/rust/xstore/include/xstore_sql.h`. Request envelope:
`{"format":"xstore-native-sql-request/v1","store":"<path>","sql":"SELECT generation_id FROM rag_generation WHERE alias_name = $1","params":["gptbridge_rag"]}`.
Returned UTF-8 JSON is engine-owned and length-delimited. Release exactly once
through the matching engine free function; do not use another allocator. SQL
execution neither invokes an executable nor accesses PostgreSQL. Mutable SQL
and transactions remain unsupported pending the governed writer protocol.