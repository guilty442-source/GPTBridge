-- 088_rag_capacity_governance.sql
-- RAG Capacity Governance — every pipeline layer has a budget.
--
-- Purpose:
--   The RAG pipeline (ingestion → embedding → retrieval → reranker → generation)
--   gets a bounded, inspectable capacity policy instead of a single top-k knob.
--   This migration is the CONTROL plane for that contract:
--
--     gptbridge_ragpolicy.capacity_policy         — per-layer upper limits
--     gptbridge_ragpolicy.queue_state             — bounded backpressure state
--                                  ACCEPTING / THROTTLED / PAUSED / DRAINING
--     gptbridge_ragpolicy.qdrant_collection_generation
--                                  — logical collection ↔ physical generation
--                                    registry with BUILDING→VERIFYING→ACTIVE
--                                    alias-swap lifecycle
--     gptbridge_ragpolicy.retrieval_trace         — agentic round-by-round trace
--                                    + stop_reason (evidence / rounds / budget)
--     gptbridge_ragpolicy.phase_latency           — phase timings for query SLO
--     gptbridge_ragpolicy.rag_slo                 — measurable RAG SLO targets
--
-- Backpressure rule encoded in queue_admission():
--   * ACCEPTING  -> new work admitted.
--   * THROTTLED  -> under threshold: admitted; at/over threshold:
--                    ingestion workloads get decision 'INDEX_PENDING'
--                    (metadata accepted, indexing deferred) instead of a
--                    timeout; embedding/reranker get 'DEFER' + retry hint.
--   * PAUSED     -> new work rejected (reason recorded).
--   * DRAINING   -> no new work; existing workers finish.
--
-- Separation of concerns (resource pools):
--   query-facing phases (query embedding, reranker, generation) must be
--   represented by interactive queues; background phases (document embedding,
--   index runner, reconciliation, rebuild) by background queues.  Priority:
--   query > reconciliation > bulk reindex.  The adaptive admission ladder
--   (shared-layer adaptive plane) maps INTERACTIVE/BACKGROUND/MAINTENANCE on
--   top of these states at runtime.
--
-- Codex basis:
--   A8/E21  — PostgreSQL: central-structured-official-data.
--   A10/E10 — explicit-allowlist; deny-by-default.

CREATE SCHEMA IF NOT EXISTS gptbridge_ragpolicy;

COMMENT ON SCHEMA gptbridge_ragpolicy IS
    'RAG capacity governance: budgets, bounded queues, generation registry, '
    'retrieval traces, phase latency, SLO targets.  Authority remains the '
    'engine tables; this is control-plane metadata only.';

GRANT USAGE ON SCHEMA gptbridge_ragpolicy TO gptbridge_index_reader;
GRANT USAGE ON SCHEMA gptbridge_ragpolicy TO gptbridge_xingcheng_reader;
GRANT USAGE ON SCHEMA gptbridge_ragpolicy TO gptbridge_index_executor;
GRANT USAGE ON SCHEMA gptbridge_ragpolicy TO gptbridge_transport_executor;

-- ============================================================================
-- capacity_policy — per-layer upper limits (the RagCapacityPolicy contract).
--   default row seeded below matches the spec budgets for a local model rig.
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_ragpolicy.capacity_policy (
    policy_version text PRIMARY KEY,
    active boolean NOT NULL DEFAULT false,
    ingestion jsonb NOT NULL DEFAULT '{}'::jsonb,
    embedding jsonb NOT NULL DEFAULT '{}'::jsonb,
    retrieval jsonb NOT NULL DEFAULT '{}'::jsonb,
    reranker jsonb NOT NULL DEFAULT '{}'::jsonb,
    generation jsonb NOT NULL DEFAULT '{}'::jsonb,
    qdrant_profiles jsonb NOT NULL DEFAULT '{}'::jsonb,
    cache jsonb NOT NULL DEFAULT '{}'::jsonb,
    tier_defaults jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- ============================================================================
-- queue_state — bounded backpressure state per pipeline queue.
--   queue_name: ingestion | query_embedding | index_embedding | reranker |
--               index_runner | reconcile | rebuild
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_ragpolicy.queue_state (
    queue_name text PRIMARY KEY
        CHECK (queue_name IN ('ingestion','query_embedding','index_embedding',
                             'reranker','index_runner','reconcile','rebuild')),
    state text NOT NULL DEFAULT 'ACCEPTING'
        CHECK (state IN ('ACCEPTING','THROTTLED','PAUSED','DRAINING')),
    threshold_depth bigint NOT NULL DEFAULT 1000,
    current_depth bigint NOT NULL DEFAULT 0,
    reason text,
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- ============================================================================
-- qdrant_collection_generation — logical alias ↔ physical generation.
--   logical_alias:  gptbridge_shared_knowledge
--   physical_name:  gptbridge_shared_knowledge_g0001 ... g000N
--   lifecycle:      BUILDING -> VERIFYING -> ACTIVE (+ alias swap intent) ->
--                   RETIRED; FAILED on aborted build.
--   The alias swap itself is performed by the Qdrant client; this registry is
--   the durable record of intent + verification so a crash before/after swap
--   is detectable and resumable.
-- ============================================================================
CREATE SEQUENCE IF NOT EXISTS gptbridge_ragpolicy.generation_serial_seq START 1;

CREATE TABLE IF NOT EXISTS gptbridge_ragpolicy.qdrant_collection_generation (
    generation_id text PRIMARY KEY,
    logical_alias text NOT NULL,
    physical_name text NOT NULL UNIQUE,
    schema_version text NOT NULL,
    embedding_model text NOT NULL,
    embedding_dimension int NOT NULL,
    state text NOT NULL DEFAULT 'BUILDING'
        CHECK (state IN ('BUILDING','VERIFYING','ACTIVE','RETIRED','FAILED')),
    initial_points bigint NOT NULL DEFAULT 0,
    verification_result jsonb,
    alias_swapped_at timestamptz,
    error_message text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (logical_alias, generation_id)
);

-- ============================================================================
-- retrieval_trace — round-by-round agentic retrieval trace.
--   stop_reason: EVIDENCE_SUFFICIENT | MAX_ROUNDS | TIME_BUDGET |
--                NO_NEW_EVIDENCE
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_ragpolicy.retrieval_trace (
    trace_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    request_id text NOT NULL,
    rag_type text NOT NULL,
    round_no int NOT NULL,
    query text NOT NULL,
    rewritten_query text,
    hit_ids jsonb NOT NULL DEFAULT '[]'::jsonb,
    evidence_score double precision,
    stop_reason text,
    module_ids jsonb DEFAULT '[]'::jsonb,
    generation_id text,
    policy_version text,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS retrieval_trace_request_idx
    ON gptbridge_ragpolicy.retrieval_trace (request_id, round_no);

-- ============================================================================
-- phase_latency — per-request phase timings (scope/embed/qdrant/fts/fusion/
--   reranker/context/generation).  Aggregate per SLO; raw rows are compact.
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_ragpolicy.phase_latency (
    latency_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    request_id text NOT NULL,
    phase text NOT NULL,
    elapsed_ms double precision NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS phase_latency_request_idx
    ON gptbridge_ragpolicy.phase_latency (request_id, phase);

-- ============================================================================
-- rag_slo — measurable RAG SLO targets.
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_ragpolicy.rag_slo (
    metric_name text PRIMARY KEY,
    target_value double precision,
    direction text NOT NULL DEFAULT '>='
        CHECK (direction IN ('>=','<=','=')),
    unit text NOT NULL DEFAULT 'count',
    description text,
    active boolean NOT NULL DEFAULT true,
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- ============================================================================
-- Row level security + grants (policy tables are control-plane metadata; the
-- keep-it-simple rule: any gptbridge_* role may read; executors may write).
-- ============================================================================
ALTER TABLE gptbridge_ragpolicy.capacity_policy ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_ragpolicy.queue_state ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_ragpolicy.qdrant_collection_generation ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_ragpolicy.retrieval_trace ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_ragpolicy.phase_latency ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_ragpolicy.rag_slo ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS ragpolicy_select ON gptbridge_ragpolicy.capacity_policy;
CREATE POLICY ragpolicy_select ON gptbridge_ragpolicy.capacity_policy
    FOR SELECT USING (true);
DROP POLICY IF EXISTS ragpolicy_write ON gptbridge_ragpolicy.capacity_policy;
CREATE POLICY ragpolicy_write ON gptbridge_ragpolicy.capacity_policy
    FOR ALL USING (current_user LIKE 'gptbridge_%')
    WITH CHECK (current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS queue_select ON gptbridge_ragpolicy.queue_state;
CREATE POLICY queue_select ON gptbridge_ragpolicy.queue_state
    FOR SELECT USING (true);
DROP POLICY IF EXISTS queue_write ON gptbridge_ragpolicy.queue_state;
CREATE POLICY queue_write ON gptbridge_ragpolicy.queue_state
    FOR ALL USING (current_user LIKE 'gptbridge_%')
    WITH CHECK (current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS gen_select ON gptbridge_ragpolicy.qdrant_collection_generation;
CREATE POLICY gen_select ON gptbridge_ragpolicy.qdrant_collection_generation
    FOR SELECT USING (true);
DROP POLICY IF EXISTS gen_write ON gptbridge_ragpolicy.qdrant_collection_generation;
CREATE POLICY gen_write ON gptbridge_ragpolicy.qdrant_collection_generation
    FOR ALL USING (current_user LIKE 'gptbridge_%')
    WITH CHECK (current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS trace_select ON gptbridge_ragpolicy.retrieval_trace;
CREATE POLICY trace_select ON gptbridge_ragpolicy.retrieval_trace
    FOR SELECT USING (true);
DROP POLICY IF EXISTS trace_write ON gptbridge_ragpolicy.retrieval_trace;
CREATE POLICY trace_write ON gptbridge_ragpolicy.retrieval_trace
    FOR ALL USING (current_user LIKE 'gptbridge_%')
    WITH CHECK (current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS latency_select ON gptbridge_ragpolicy.phase_latency;
CREATE POLICY latency_select ON gptbridge_ragpolicy.phase_latency
    FOR SELECT USING (true);
DROP POLICY IF EXISTS latency_write ON gptbridge_ragpolicy.phase_latency;
CREATE POLICY latency_write ON gptbridge_ragpolicy.phase_latency
    FOR ALL USING (current_user LIKE 'gptbridge_%')
    WITH CHECK (current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS slo_select ON gptbridge_ragpolicy.rag_slo;
CREATE POLICY slo_select ON gptbridge_ragpolicy.rag_slo
    FOR SELECT USING (true);
DROP POLICY IF EXISTS slo_write ON gptbridge_ragpolicy.rag_slo;
CREATE POLICY slo_write ON gptbridge_ragpolicy.rag_slo
    FOR ALL USING (current_user LIKE 'gptbridge_%')
    WITH CHECK (current_user LIKE 'gptbridge_%');

REVOKE ALL ON gptbridge_ragpolicy.capacity_policy FROM PUBLIC;
REVOKE ALL ON gptbridge_ragpolicy.queue_state FROM PUBLIC;
REVOKE ALL ON gptbridge_ragpolicy.qdrant_collection_generation FROM PUBLIC;
REVOKE ALL ON gptbridge_ragpolicy.retrieval_trace FROM PUBLIC;
REVOKE ALL ON gptbridge_ragpolicy.phase_latency FROM PUBLIC;
REVOKE ALL ON gptbridge_ragpolicy.rag_slo FROM PUBLIC;

GRANT SELECT ON gptbridge_ragpolicy.capacity_policy TO gptbridge_index_reader;
GRANT SELECT ON gptbridge_ragpolicy.queue_state TO gptbridge_index_reader;
GRANT SELECT ON gptbridge_ragpolicy.qdrant_collection_generation TO gptbridge_index_reader;
GRANT SELECT ON gptbridge_ragpolicy.retrieval_trace TO gptbridge_index_reader;
GRANT SELECT ON gptbridge_ragpolicy.phase_latency TO gptbridge_index_reader;
GRANT SELECT ON gptbridge_ragpolicy.rag_slo TO gptbridge_index_reader;
GRANT SELECT ON gptbridge_ragpolicy.capacity_policy TO gptbridge_xingcheng_reader;
GRANT SELECT ON gptbridge_ragpolicy.queue_state TO gptbridge_xingcheng_reader;
GRANT SELECT ON gptbridge_ragpolicy.qdrant_collection_generation TO gptbridge_xingcheng_reader;
GRANT SELECT ON gptbridge_ragpolicy.retrieval_trace TO gptbridge_xingcheng_reader;
GRANT SELECT ON gptbridge_ragpolicy.phase_latency TO gptbridge_xingcheng_reader;
GRANT SELECT ON gptbridge_ragpolicy.rag_slo TO gptbridge_xingcheng_reader;

GRANT SELECT, INSERT, UPDATE, DELETE ON gptbridge_ragpolicy.capacity_policy
    TO gptbridge_index_executor;
GRANT SELECT, INSERT, UPDATE, DELETE ON gptbridge_ragpolicy.queue_state
    TO gptbridge_index_executor, gptbridge_transport_executor;
GRANT SELECT, INSERT, UPDATE, DELETE ON gptbridge_ragpolicy.qdrant_collection_generation
    TO gptbridge_index_executor;
GRANT SELECT, INSERT, UPDATE, DELETE ON gptbridge_ragpolicy.retrieval_trace
    TO gptbridge_index_executor;
GRANT SELECT, INSERT, UPDATE, DELETE ON gptbridge_ragpolicy.phase_latency
    TO gptbridge_index_executor, gptbridge_transport_executor;
GRANT SELECT, INSERT, UPDATE, DELETE ON gptbridge_ragpolicy.rag_slo
    TO gptbridge_index_executor;

-- ============================================================================
-- Seed: default capacity policy (spec budgets for a local rig).
-- ============================================================================
INSERT INTO gptbridge_ragpolicy.capacity_policy (
    policy_version, active, ingestion, embedding, retrieval, reranker,
    generation, qdrant_profiles, cache, tier_defaults
) VALUES (
    'rag-capacity-v1', true,
    '{"max_files_per_job": 64, "max_bytes_per_file": 41943040, '
    '"max_chunks_per_resource": 512, "max_chunks_per_batch": 32, '
    '"max_pending_index_jobs": 500}'::jsonb,
    '{"batch_size": 8, "max_parallel_batches": 4, "max_queue_depth": 512, '
    '"timeout_ms": 30000}'::jsonb,
    '{"dense_candidate_limit": 30, "sparse_candidate_limit": 30, '
    '"graph_candidate_limit": 20, "memory_candidate_limit": 10}'::jsonb,
    '{"max_candidates": 40, "batch_size": 16, "timeout_ms": 20000}'::jsonb,
    '{"max_context_tokens": 8000, "max_chunks": 20, '
    '"max_chunks_per_resource": 3, "generation_timeout_ms": 120000}'::jsonb,
    '{"small": {"m": 16, "ef_construct": 100, "max_points": 100000}, '
    '"medium": {"m": 32, "ef_construct": 200, "max_points": 1000000}, '
    '"large": {"m": 64, "ef_construct": 400, "max_points": 10000000}}'::jsonb,
    '{"embedding_ttl_seconds": 86400, "retrieval_ttl_seconds": 60, '
    '"reranker_results_ttl_seconds": 60, "cache_final_answer": false}'::jsonb,
    '{"hot": {"ttl_seconds": 2592000}, "warm": {"ttl_seconds": 7776000}, '
    '"cold": {"ttl_seconds": null, "search_by_default": false}}'::jsonb
)
ON CONFLICT (policy_version) DO NOTHING;

-- ============================================================================
-- Seed: RAG SLO targets (measurable; not necessarily met on day one).
-- ============================================================================
INSERT INTO gptbridge_ragpolicy.rag_slo (metric_name, target_value, direction, unit, description) VALUES
    ('canonical_retrieval_availability',   0.99,     '>=', 'ratio',    'Canonical retrieval availability'),
    ('p95_retrieval_latency_ms',           500.0,    '<=', 'ms',       'p95 retrieval latency excl. generation'),
    ('zero_result_rate',                   0.0,      '<=', 'ratio',    'Fraction of queries with zero hits'),
    ('unauthorized_hit_rate',              0.0,      '<=', 'ratio',    'Hits served despite unauthorized scope'),
    ('cross_module_leakage',               0,        '<=', 'count',    'Cross-module leaks (must be zero)'),
    ('canonical_inconsistency_rate',       0.001,    '<=', 'ratio',    'Canonical index inconsistency rate'),
    ('reconciliation_backlog',             1000,     '<=', 'count',    'Hard cap on reconciliation backlog')
ON CONFLICT (metric_name) DO NOTHING;

-- ============================================================================
-- set_queue_state(p_queue, p_state, p_threshold, p_reason)
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_ragpolicy.set_queue_state(
    p_queue text,
    p_state text,
    p_threshold bigint DEFAULT 1000,
    p_reason text DEFAULT NULL
) RETURNS jsonb AS $$
BEGIN
    IF p_queue NOT IN ('ingestion','query_embedding','index_embedding',
                       'reranker','index_runner','reconcile','rebuild') THEN
        RAISE EXCEPTION 'unknown queue: %', p_queue;
    END IF;
    IF p_state NOT IN ('ACCEPTING','THROTTLED','PAUSED','DRAINING') THEN
        RAISE EXCEPTION 'unknown state: %', p_state;
    END IF;
    INSERT INTO gptbridge_ragpolicy.queue_state
        (queue_name, state, threshold_depth, reason)
    VALUES (p_queue, p_state, p_threshold, p_reason)
    ON CONFLICT (queue_name) DO UPDATE SET
        state = EXCLUDED.state,
        threshold_depth = EXCLUDED.threshold_depth,
        reason = EXCLUDED.reason,
        updated_at = now();
    RETURN jsonb_build_object('queue_name', p_queue, 'state', p_state,
                              'threshold_depth', p_threshold, 'reason', p_reason);
END;
$$ LANGUAGE plpgsql;

-- ============================================================================
-- queue_admission(p_queue, p_depth, p_workload)
--   Backpressure decision per the spec.  p_workload: 'ingestion' |
--   'embedding' | 'reranker' | 'reconcile' | 'rebuild' | 'query'
--   Returns:
--     ACCEPT         — new work admitted.
--     INDEX_PENDING  — ingestion: accept metadata, defer indexing (never a
--                      timeout while embedding_queue over its threshold).
--     DEFER          — retry with retry_after_seconds.
--     REJECT         — no new work (PAUSED/DRAINING).
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_ragpolicy.queue_admission(
    p_queue text,
    p_depth bigint,
    p_workload text DEFAULT 'ingestion'
) RETURNS jsonb AS $$
DECLARE
    v_state text;
    v_threshold bigint;
    v_reason text;
BEGIN
    SELECT state, threshold_depth, reason INTO v_state, v_threshold, v_reason
    FROM gptbridge_ragpolicy.queue_state WHERE queue_name = p_queue;
    IF v_state IS NULL THEN
        v_state := 'ACCEPTING';
        v_threshold := 1000;
    END IF;

    UPDATE gptbridge_ragpolicy.queue_state
    SET current_depth = p_depth, updated_at = now()
    WHERE queue_name = p_queue;

    IF v_state = 'PAUSED' THEN
        RETURN jsonb_build_object('decision', 'REJECT', 'queue', p_queue,
                                  'state', v_state, 'depth', p_depth,
                                  'reason', COALESCE(v_reason, 'queue paused'));
    END IF;
    IF v_state = 'DRAINING' THEN
        RETURN jsonb_build_object('decision', 'REJECT', 'queue', p_queue,
                                  'state', v_state, 'depth', p_depth,
                                  'reason', 'queue draining; no new work');
    END IF;
    IF v_state = 'ACCEPTING' THEN
        RETURN jsonb_build_object('decision', 'ACCEPT', 'queue', p_queue,
                                  'state', v_state, 'depth', p_depth,
                                  'reason', NULL);
    END IF;
    -- THROTTLED
    IF p_depth < v_threshold THEN
        RETURN jsonb_build_object('decision', 'ACCEPT', 'queue', p_queue,
                                  'state', v_state, 'depth', p_depth,
                                  'reason', 'below threshold',
                                  'threshold', v_threshold);
    END IF;
    IF p_workload = 'ingestion' THEN
        RETURN jsonb_build_object('decision', 'INDEX_PENDING', 'queue', p_queue,
                                  'state', v_state, 'depth', p_depth,
                                  'reason', 'embedding queue over threshold; '
                                            'accept metadata, defer indexing',
                                  'threshold', v_threshold);
    END IF;
    RETURN jsonb_build_object('decision', 'DEFER', 'queue', p_queue,
                              'state', v_state, 'depth', p_depth,
                              'reason', 'queue over threshold',
                              'retry_after_seconds', 2.0,
                              'threshold', v_threshold);
END;
$$ LANGUAGE plpgsql;

-- ============================================================================
-- record_phase_latency(p_request_id, p_phase, p_elapsed_ms)
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_ragpolicy.record_phase_latency(
    p_request_id text,
    p_phase text,
    p_elapsed_ms double precision
) RETURNS bigint AS $$
DECLARE
    v_id bigint;
BEGIN
    INSERT INTO gptbridge_ragpolicy.phase_latency (request_id, phase, elapsed_ms)
    VALUES (p_request_id, p_phase, p_elapsed_ms)
    RETURNING latency_id INTO v_id;
    RETURN v_id;
END;
$$ LANGUAGE plpgsql;

-- ============================================================================
-- record_retrieval_trace(...) — one round of an agentic retrieval trace.
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_ragpolicy.record_retrieval_trace(
    p_request_id text,
    p_rag_type text,
    p_round_no int,
    p_query text,
    p_rewritten_query text DEFAULT NULL,
    p_hit_ids jsonb DEFAULT '[]'::jsonb,
    p_evidence_score double precision DEFAULT NULL,
    p_stop_reason text DEFAULT NULL,
    p_module_ids jsonb DEFAULT '[]'::jsonb,
    p_generation_id text DEFAULT NULL,
    p_policy_version text DEFAULT NULL
) RETURNS bigint AS $$
DECLARE
    v_id bigint;
BEGIN
    INSERT INTO gptbridge_ragpolicy.retrieval_trace (
        request_id, rag_type, round_no, query, rewritten_query, hit_ids,
        evidence_score, stop_reason, module_ids, generation_id, policy_version
    ) VALUES (
        p_request_id, p_rag_type, p_round_no, p_query, p_rewritten_query,
        p_hit_ids, p_evidence_score, p_stop_reason, p_module_ids,
        p_generation_id, p_policy_version
    ) RETURNING trace_id INTO v_id;
    RETURN v_id;
END;
$$ LANGUAGE plpgsql;

-- ============================================================================
-- Generation lifecycle (logical collection ↔ physical generation + alias swap)
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_ragpolicy.initialize_generation(
    p_logical_alias text,
    p_schema_version text,
    p_embedding_model text,
    p_embedding_dimension int
) RETURNS jsonb AS $$
DECLARE
    v_serial bigint;
    v_generation text;
    v_physical text;
BEGIN
    SELECT nextval('gptbridge_ragpolicy.generation_serial_seq') INTO v_serial;
    v_generation := 'g' || lpad(v_serial::text, 4, '0');
    v_physical := p_logical_alias || '_' || v_generation;
    INSERT INTO gptbridge_ragpolicy.qdrant_collection_generation (
        generation_id, logical_alias, physical_name, schema_version,
        embedding_model, embedding_dimension, state
    ) VALUES (
        v_generation, p_logical_alias, v_physical, p_schema_version,
        p_embedding_model, p_embedding_dimension, 'BUILDING'
    );
    RETURN jsonb_build_object('generation_id', v_generation,
                              'logical_alias', p_logical_alias,
                              'physical_name', v_physical,
                              'state', 'BUILDING');
END;
$$ LANGUAGE plpgsql;

-- ============================================================================
-- advance_generation(p_generation, p_target, p_verification)
--   BUILDING -> VERIFYING -> ACTIVE (alias-swap intent) -> RETIRED.
--   Guards: only ACTIVE may follow VERIFYING; alias swap is recorded as
--   intent (alias_swapped_at); RETIRE is safe at any later point.  FAILED may
--   be recorded explicitly out of BUILDING/VERIFYING.
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_ragpolicy.advance_generation(
    p_generation text,
    p_target text,
    p_verification jsonb DEFAULT NULL
) RETURNS jsonb AS $$
DECLARE
    v_state text;
    v_alias text;
    v_result jsonb;
BEGIN
    SELECT state, logical_alias INTO v_state, v_alias
    FROM gptbridge_ragpolicy.qdrant_collection_generation
    WHERE generation_id = p_generation;
    IF v_state IS NULL THEN
        RAISE EXCEPTION 'unknown generation: %', p_generation;
    END IF;

    IF v_state = 'BUILDING' AND p_target = 'VERIFYING' THEN
        UPDATE gptbridge_ragpolicy.qdrant_collection_generation
        SET state = 'VERIFYING', updated_at = now()
        WHERE generation_id = p_generation;
        v_result := jsonb_build_object('generation_id', p_generation,
                                       'state', 'VERIFYING');
    ELSIF v_state = 'VERIFYING' AND p_target = 'ACTIVE' THEN
        UPDATE gptbridge_ragpolicy.qdrant_collection_generation
        SET state = 'ACTIVE', verification_result = p_verification,
            alias_swapped_at = now(), updated_at = now()
        WHERE generation_id = p_generation;
        -- Retire the previous ACTIVE generation for the same logical alias.
        UPDATE gptbridge_ragpolicy.qdrant_collection_generation
        SET state = 'RETIRED', updated_at = now()
        WHERE logical_alias = v_alias AND state = 'ACTIVE'
          AND generation_id <> p_generation;
        v_result := jsonb_build_object('generation_id', p_generation,
                                       'state', 'ACTIVE',
                                       'alias_swapped_at', now(),
                                       'alias', v_alias);
    ELSIF (v_state IN ('BUILDING','VERIFYING','ACTIVE')) AND p_target = 'RETIRED' THEN
        UPDATE gptbridge_ragpolicy.qdrant_collection_generation
        SET state = 'RETIRED', updated_at = now()
        WHERE generation_id = p_generation;
        v_result := jsonb_build_object('generation_id', p_generation,
                                       'state', 'RETIRED');
    ELSIF v_state IN ('BUILDING','VERIFYING') AND p_target = 'FAILED' THEN
        UPDATE gptbridge_ragpolicy.qdrant_collection_generation
        SET state = 'FAILED', error_message = p_verification ->> 'error',
            updated_at = now()
        WHERE generation_id = p_generation;
        v_result := jsonb_build_object('generation_id', p_generation,
                                       'state', 'FAILED');
    ELSE
        RAISE EXCEPTION 'invalid generation transition: % -> %', v_state, p_target;
    END IF;
    RETURN v_result;
END;
$$ LANGUAGE plpgsql;

-- ============================================================================
-- current_capacity_policy() — active policy as a single JSONB document.
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_ragpolicy.current_capacity_policy()
RETURNS jsonb AS $$
DECLARE
    v_row gptbridge_ragpolicy.capacity_policy%ROWTYPE;
BEGIN
    SELECT * INTO v_row FROM gptbridge_ragpolicy.capacity_policy
    WHERE active = true ORDER BY updated_at DESC LIMIT 1;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;
    RETURN jsonb_build_object(
        'policy_version', v_row.policy_version,
        'ingestion', v_row.ingestion,
        'embedding', v_row.embedding,
        'retrieval', v_row.retrieval,
        'reranker', v_row.reranker,
        'generation', v_row.generation,
        'qdrant_profiles', v_row.qdrant_profiles,
        'cache', v_row.cache,
        'tier_defaults', v_row.tier_defaults,
        'updated_at', to_char(v_row.updated_at, 'YYYY-MM-DD"T"HH24:MI:SSOF')
    );
END;
$$ LANGUAGE plpgsql STABLE;

-- ============================================================================
-- upsert_capacity_policy(p_policy_version, p_ingestion, p_embedding,
--   p_retrieval, p_reranker, p_generation, p_profiles, p_cache, p_tiers)
--   Deactivates other rows; the given version becomes the active policy.
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_ragpolicy.upsert_capacity_policy(
    p_policy_version text,
    p_ingestion jsonb,
    p_embedding jsonb,
    p_retrieval jsonb,
    p_reranker jsonb,
    p_generation jsonb,
    p_profiles jsonb DEFAULT '{}'::jsonb,
    p_cache jsonb DEFAULT '{}'::jsonb,
    p_tiers jsonb DEFAULT '{}'::jsonb
) RETURNS text AS $$
BEGIN
    UPDATE gptbridge_ragpolicy.capacity_policy SET active = false;
    INSERT INTO gptbridge_ragpolicy.capacity_policy (
        policy_version, active, ingestion, embedding, retrieval, reranker,
        generation, qdrant_profiles, cache, tier_defaults
    ) VALUES (
        p_policy_version, true, p_ingestion, p_embedding, p_retrieval,
        p_reranker, p_generation, p_profiles, p_cache, p_tiers
    )
    ON CONFLICT (policy_version) DO UPDATE SET
        active = true,
        ingestion = EXCLUDED.ingestion,
        embedding = EXCLUDED.embedding,
        retrieval = EXCLUDED.retrieval,
        reranker = EXCLUDED.reranker,
        generation = EXCLUDED.generation,
        qdrant_profiles = EXCLUDED.qdrant_profiles,
        cache = EXCLUDED.cache,
        tier_defaults = EXCLUDED.tier_defaults,
        updated_at = now();
    RETURN p_policy_version;
END;
$$ LANGUAGE plpgsql;

-- ============================================================================
-- list_rag_slo() — active SLO targets.
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_ragpolicy.list_rag_slo()
RETURNS TABLE (metric_name text, target_value double precision, direction text,
               unit text, description text) AS $$
BEGIN
    RETURN QUERY
    SELECT s.metric_name, s.target_value, s.direction, s.unit, s.description
    FROM gptbridge_ragpolicy.rag_slo s
    WHERE s.active = true
    ORDER BY s.metric_name;
END;
$$ LANGUAGE plpgsql STABLE;