# ragd — Contract `ragd/v1`

Governed Rust RAG orchestration service (A610 PYTHON-WORK-TRANSFER:
RAG→rust). Loopback-only HTTP/JSON; refuses non-loopback binds.

## Authority boundary

ragd owns **retrieval orchestration only**. PostgreSQL remains the sole
canonical authority: every vector candidate returned by vectord is
re-proved against `gptbridge_rag.chunk` ⨝ `gptbridge_index.resource`
(`index_status NOT IN tombstoned/deleted/purged` ∧ `NOT EXISTS
gptbridge_rag.tombstone purged=false`) and `gptbridge_rag.index_state`
(`status IN indexed, active`), then filtered by governed module scope and
the ACTIVE generation — a direct port of
`core_system/rag/pipeline.py::_apply_read_barrier`.

Fail-closed: unreachable vectord → `CANDIDATE_FETCH_FAILED`; unreachable
or erroring PostgreSQL → zero hits with `authority_error` set (a hit is
never returned unproved). An empty `module_ids` scope is rejected.

## Routes

- `GET /healthz` → `{ok, contract, service:"ragd", version, vectord,
  dsn_configured, uptime_s, cag}`
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

## Retrieval lanes (retrieve.rs)

- `hybrid` : vectord dense (barrier-proved) + PG FTS
  (`gptbridge_rag.chunk` `to_tsvector`/`plainto_tsquery`, `index_state`
  proof) → `channel_fusion_hybrid` RRF
- `code`   : dense + FTS → RRF + symbol boost → CODE_SNIPPET
- `memory` : dense + FTS → RRF → memory-scope/session filter +
  session boost → `memory_score` composite
- `agentic`: declared; no lane wired (bounded rounds reserved)

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
- `--dsn` (else env `GPTBRIDGE_POSTGRES_DSN`); `credman:`-prefixed values
  resolve through the Windows credential store — mirrors
  `shared_layer/security/dsn_policy.py` (G89).

## Registry

`architecture_registry.json` component `ragd`
(execution/standalone-service, on-demand, non-canonical — orchestration
holds no formal authority). Binary: `bin/ragd.exe` (build artifact,
gitignored; `cargo build --release`).
