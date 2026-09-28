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
  dsn_configured, uptime_s}`
- `POST /v1/rag/query` —
  `{collection, module_ids[], text|vector, top_k?, alias?,
  generation_id?}` → `{ok, contract, hits[], candidates,
  generation_id, dropped{unauthorized, generation_mismatch,
  missing_metadata, tombstoned}, authority_error}`

`generation_id` omitted → resolved from
`gptbridge_rag.generation WHERE state='ACTIVE'` for `alias`
(default `gptbridge_rag`).

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
