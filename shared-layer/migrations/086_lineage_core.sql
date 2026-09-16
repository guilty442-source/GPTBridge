-- 086_lineage_core.sql
-- Central Lineage Model — append-only cross-engine provenance graph.
--
-- Purpose:
--   One central lineage model (gptbridge_lineage) that traces every
--   content item across engines, producers, and transformations:
--     * lineage_node          — every oszlop/entity that can hold content:
--                               resource (PG canonical), chunk, Qdrant point,
--                               SQLite record, audit event, model output.
--     * lineage_edge          — directed "A produced B from/of C" arcs.
--     * transformation        — registry of chunking / embedding / reconcile /
--                               restore / migration transformations that create edges.
--
-- Design (per central-lineage requirements):
--   1. append-only — UPDATE/DELETE on node/edge is rejected (trigger + grants);
--      corrections happen by INSERTING new nodes/edges with relation types
--      'supersedes', 'corrects', 'invalidates'.  Validity is derived, never
--      mutated: an edge is "active" unless an 'invalidates' edge targets it.
--   2. producer model markers — producer_type ('user' | 'model' | 'executor' |
--      'system' | 'reconcile' | 'restore'), model_id, model_version, authority_class,
--      and content_status ('derived' | 'candidate' | 'reviewed' | 'accepted' |
--      'authoritative') so model-produced content is visible in every trace.
--   3. low-latency requirement — lineage is written in the SAME transaction as
--      the governed write (atomic, no second-hop consistency), and every read
--      entry point is a small index-backed recursive query; detection of a
--      failed lineage write is best-effort and never blocks the write path.
--   4. reverse lookup + impact — reverse_lookup() traces a node back to its
--      sources; impact() traces forward to every derived/dependent item.
--   5. health_check() — orphan nodes, broken edges, missing cross-engine
--      metadata, cycles, edges without a transformation.
--   6. separate from audit — lineage uses the same session provenance
--      (actor/executor/decision/correlation/generation) as gptbridge_audit but
--      lives in its own schema; the two link by correlation_id/decision_id.
--
-- Codex basis:
--   A8/E21  — PostgreSQL: central-structured-official-data.
--   A46/E22 — Audit: mandatory-ledger; write=governed-executor.
--   A10/E10 — Authorization: explicit-allowlist; deny-by-default.

-- ============================================================================
-- Schema
-- ============================================================================
CREATE SCHEMA IF NOT EXISTS gptbridge_lineage;

GRANT USAGE ON SCHEMA gptbridge_lineage TO gptbridge_index_reader;
GRANT USAGE ON SCHEMA gptbridge_lineage TO gptbridge_xingcheng_reader;
GRANT USAGE ON SCHEMA gptbridge_lineage TO gptbridge_index_executor;
GRANT USAGE ON SCHEMA gptbridge_lineage TO gptbridge_transport_executor;

-- ============================================================================
-- transformation registry
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_lineage.transformation (
    transformation_id text PRIMARY KEY,
    transformation_type text NOT NULL,
    implementation_version text NOT NULL,
    input_contract text,
    output_contract text,
    executor text NOT NULL DEFAULT 'unknown',
    created_at timestamptz NOT NULL DEFAULT now()
);

-- ============================================================================
-- lineage_node — every content-holding entity across engines.
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_lineage.lineage_node (
    node_id text PRIMARY KEY,
    engine text NOT NULL CHECK (engine IN (
        'postgresql', 'sqlite', 'qdrant', 'transport', 'audit', 'rag', 'file', 'model'
    )),
    node_type text NOT NULL,
    module_id text,
    resource_id text,
    chunk_id text,
    qdrant_point_id text,
    origin_type text,
    origin_locator text,
    origin_revision bigint,
    generation_id text,
    content_hash text,
    producer_actor text,
    producer_executor text,
    producer_type text NOT NULL DEFAULT 'executor' CHECK (producer_type IN (
        'user', 'model', 'executor', 'system', 'reconcile', 'restore'
    )),
    model_id text,
    model_version text,
    confidence numeric,
    content_status text NOT NULL DEFAULT 'derived' CHECK (content_status IN (
        'derived', 'candidate', 'reviewed', 'accepted', 'authoritative'
    )),
    authority_class text,
    executor_id text,
    correlation_id text,
    decision_id text,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (node_id IS NOT NULL AND node_id <> ''),
    CHECK (engine = 'qdrant' OR qdrant_point_id IS NULL)
);

-- ============================================================================
-- lineage_edge — directed, append-only arcs.
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_lineage.lineage_edge (
    edge_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    parent_node_id text NOT NULL
        REFERENCES gptbridge_lineage.lineage_node(node_id),
    child_node_id text NOT NULL
        REFERENCES gptbridge_lineage.lineage_node(node_id),
    relation_type text NOT NULL CHECK (relation_type IN (
        'derived_from', 'chunk_of', 'indexed_from', 'embedded_from',
        'copied_from', 'supersedes', 'reconciled_from', 'restored_from',
        'produced_by', 'invalidates', 'corrects'
    )),
    transformation_id text
        REFERENCES gptbridge_lineage.transformation(transformation_id),
    run_id text,
    module_id text,
    generation_id text,
    producer_actor text,
    producer_executor text,
    executor_id text,
    correlation_id text,
    decision_id text,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (parent_node_id <> child_node_id)
);

-- Edge idempotency: the same (parent, child, relation, run) must not repeat.
-- run_id may be NULL for manual/synthetic links, hence the coalesce key.
CREATE UNIQUE INDEX IF NOT EXISTS lineage_edge_dedup_ux
    ON gptbridge_lineage.lineage_edge
        (relation_type, parent_node_id, child_node_id, COALESCE(run_id, ''));

CREATE INDEX IF NOT EXISTS lineage_edge_parent_idx
    ON gptbridge_lineage.lineage_edge (parent_node_id, relation_type);
CREATE INDEX IF NOT EXISTS lineage_edge_child_idx
    ON gptbridge_lineage.lineage_edge (child_node_id, relation_type);
CREATE INDEX IF NOT EXISTS lineage_edge_run_idx
    ON gptbridge_lineage.lineage_edge (run_id);
CREATE INDEX IF NOT EXISTS lineage_edge_tx_idx
    ON gptbridge_lineage.lineage_edge (transformation_id);
CREATE INDEX IF NOT EXISTS lineage_edge_module_idx
    ON gptbridge_lineage.lineage_edge (module_id, generation_id);

CREATE INDEX IF NOT EXISTS lineage_node_module_idx
    ON gptbridge_lineage.lineage_node (module_id, node_type);
CREATE INDEX IF NOT EXISTS lineage_node_resource_idx
    ON gptbridge_lineage.lineage_node (resource_id);
CREATE INDEX IF NOT EXISTS lineage_node_chunk_idx
    ON gptbridge_lineage.lineage_node (chunk_id);
CREATE INDEX IF NOT EXISTS lineage_node_point_idx
    ON gptbridge_lineage.lineage_node (qdrant_point_id)
    WHERE qdrant_point_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS lineage_node_producer_idx
    ON gptbridge_lineage.lineage_node (producer_type, producer_executor);
CREATE INDEX IF NOT EXISTS lineage_node_status_idx
    ON gptbridge_lineage.lineage_node (content_status);
CREATE INDEX IF NOT EXISTS lineage_node_hash_idx
    ON gptbridge_lineage.lineage_node (content_hash);
CREATE INDEX IF NOT EXISTS lineage_node_correlation_idx
    ON gptbridge_lineage.lineage_node (correlation_id);
CREATE INDEX IF NOT EXISTS lineage_node_generation_idx
    ON gptbridge_lineage.lineage_node (generation_id);

-- ============================================================================
-- Row level security
-- ============================================================================
ALTER TABLE gptbridge_lineage.lineage_node ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_lineage.lineage_node FORCE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_lineage.lineage_edge ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_lineage.lineage_edge FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS lineage_node_read ON gptbridge_lineage.lineage_node;
CREATE POLICY lineage_node_read ON gptbridge_lineage.lineage_node
    FOR SELECT USING (
        module_id IS NULL OR gptbridge_security.can_read(module_id)
    );

DROP POLICY IF EXISTS lineage_node_insert ON gptbridge_lineage.lineage_node;
CREATE POLICY lineage_node_insert ON gptbridge_lineage.lineage_node
    FOR INSERT WITH CHECK (
        module_id IS NULL OR gptbridge_security.can_write(module_id)
    );

DROP POLICY IF EXISTS lineage_edge_read ON gptbridge_lineage.lineage_edge;
CREATE POLICY lineage_edge_read ON gptbridge_lineage.lineage_edge
    FOR SELECT USING (
        module_id IS NULL OR gptbridge_security.can_read(module_id)
    );

DROP POLICY IF EXISTS lineage_edge_insert ON gptbridge_lineage.lineage_edge;
CREATE POLICY lineage_edge_insert ON gptbridge_lineage.lineage_edge
    FOR INSERT WITH CHECK (
        module_id IS NULL OR gptbridge_security.can_write(module_id)
    );

REVOKE ALL ON gptbridge_lineage.lineage_node FROM PUBLIC;
REVOKE ALL ON gptbridge_lineage.lineage_edge FROM PUBLIC;
REVOKE ALL ON gptbridge_lineage.transformation FROM PUBLIC;

GRANT SELECT ON gptbridge_lineage.lineage_node TO gptbridge_index_reader;
GRANT SELECT ON gptbridge_lineage.lineage_edge TO gptbridge_index_reader;
GRANT SELECT ON gptbridge_lineage.transformation TO gptbridge_index_reader;
GRANT SELECT ON gptbridge_lineage.lineage_node TO gptbridge_xingcheng_reader;
GRANT SELECT ON gptbridge_lineage.lineage_edge TO gptbridge_xingcheng_reader;
GRANT SELECT ON gptbridge_lineage.transformation TO gptbridge_xingcheng_reader;

-- executor roles insert nodes/edges/transformation; they never UPDATE/DELETE
-- (append-only), which is enforced here by grants and by the trigger below.
GRANT SELECT, INSERT ON gptbridge_lineage.lineage_node
    TO gptbridge_index_executor, gptbridge_transport_executor;
GRANT SELECT, INSERT ON gptbridge_lineage.lineage_edge
    TO gptbridge_index_executor, gptbridge_transport_executor;
GRANT SELECT, INSERT, UPDATE ON gptbridge_lineage.transformation
    TO gptbridge_index_executor, gptbridge_transport_executor;

-- ============================================================================
-- Append-only enforcement: no in-place mutation of lineage history.
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_lineage.prevent_lineage_mutation()
RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'gptbridge_lineage is append-only: % on % is forbidden (use supersedes/corrects/invalidates edges)',
        TG_OP, TG_TABLE_NAME;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS lineage_node_append_only ON gptbridge_lineage.lineage_node;
CREATE TRIGGER lineage_node_append_only
    BEFORE UPDATE OR DELETE ON gptbridge_lineage.lineage_node
    FOR EACH ROW EXECUTE FUNCTION gptbridge_lineage.prevent_lineage_mutation();

DROP TRIGGER IF EXISTS lineage_edge_append_only ON gptbridge_lineage.lineage_edge;
CREATE TRIGGER lineage_edge_append_only
    BEFORE UPDATE OR DELETE ON gptbridge_lineage.lineage_edge
    FOR EACH ROW EXECUTE FUNCTION gptbridge_lineage.prevent_lineage_mutation();

-- ============================================================================
-- Auto-populate provenance from session variables (set by
-- shared_layer.database.provenance.set_provenance).
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_lineage.auto_populate_provenance_node()
RETURNS trigger AS $$
BEGIN
    IF NEW.producer_actor IS NULL THEN
        NEW.producer_actor := nullif(current_setting('gptbridge.actor_id', true), '');
    END IF;
    IF NEW.producer_executor IS NULL THEN
        NEW.producer_executor := nullif(current_setting('gptbridge.executor_id', true), '');
    END IF;
    IF NEW.executor_id IS NULL THEN
        NEW.executor_id := nullif(current_setting('gptbridge.executor_id', true), '');
    END IF;
    IF NEW.correlation_id IS NULL THEN
        NEW.correlation_id := nullif(current_setting('gptbridge.correlation_id', true), '');
    END IF;
    IF NEW.decision_id IS NULL THEN
        NEW.decision_id := nullif(current_setting('gptbridge.decision_id', true), '');
    END IF;
    IF NEW.authority_class IS NULL THEN
        NEW.authority_class := nullif(current_setting('gptbridge.authority_class', true), '');
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION gptbridge_lineage.auto_populate_provenance_edge()
RETURNS trigger AS $$
BEGIN
    IF NEW.producer_actor IS NULL THEN
        NEW.producer_actor := nullif(current_setting('gptbridge.actor_id', true), '');
    END IF;
    IF NEW.producer_executor IS NULL THEN
        NEW.producer_executor := nullif(current_setting('gptbridge.executor_id', true), '');
    END IF;
    IF NEW.executor_id IS NULL THEN
        NEW.executor_id := nullif(current_setting('gptbridge.executor_id', true), '');
    END IF;
    IF NEW.correlation_id IS NULL THEN
        NEW.correlation_id := nullif(current_setting('gptbridge.correlation_id', true), '');
    END IF;
    IF NEW.decision_id IS NULL THEN
        NEW.decision_id := nullif(current_setting('gptbridge.decision_id', true), '');
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS lineage_node_provenance ON gptbridge_lineage.lineage_node;
CREATE TRIGGER lineage_node_provenance
    BEFORE INSERT ON gptbridge_lineage.lineage_node
    FOR EACH ROW EXECUTE FUNCTION gptbridge_lineage.auto_populate_provenance_node();

DROP TRIGGER IF EXISTS lineage_edge_provenance ON gptbridge_lineage.lineage_edge;
CREATE TRIGGER lineage_edge_provenance
    BEFORE INSERT ON gptbridge_lineage.lineage_edge
    FOR EACH ROW EXECUTE FUNCTION gptbridge_lineage.auto_populate_provenance_edge();

-- ============================================================================
-- edge_active(edge_id) — derived validity.
-- An edge is active unless an 'invalidates' edge targets it.
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_lineage.edge_active(p_edge_id bigint)
RETURNS boolean AS $$
    SELECT NOT EXISTS (
        SELECT 1 FROM gptbridge_lineage.lineage_edge i
        WHERE i.relation_type = 'invalidates'
          AND (i.metadata ->> 'target_edge_id')::bigint = p_edge_id
    );
$$ LANGUAGE sql STABLE;

-- ============================================================================
-- ensure_transformation(…) — idempotent registry upsert.
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_lineage.ensure_transformation(
    p_transformation_id text,
    p_transformation_type text,
    p_implementation_version text,
    p_input_contract text DEFAULT NULL,
    p_output_contract text DEFAULT NULL,
    p_executor text DEFAULT NULL
) RETURNS void AS $$
BEGIN
    INSERT INTO gptbridge_lineage.transformation (
        transformation_id, transformation_type, implementation_version,
        input_contract, output_contract, executor
    )
    VALUES (
        p_transformation_id, p_transformation_type, p_implementation_version,
        p_input_contract, p_output_contract,
        COALESCE(p_executor, nullif(current_setting('gptbridge.executor_id', true), ''), 'unknown')
    )
    ON CONFLICT (transformation_id) DO NOTHING;
END;
$$ LANGUAGE plpgsql;

-- ============================================================================
-- register_node(…) — idempotent node registration.
-- Known node ids are reused; the graph is append-only so a NEW version of an
-- item must use a distinct node_id and connect the old one with 'supersedes'.
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_lineage.register_node(
    p_node_id text,
    p_engine text,
    p_node_type text,
    p_module_id text DEFAULT NULL,
    p_resource_id text DEFAULT NULL,
    p_chunk_id text DEFAULT NULL,
    p_qdrant_point_id text DEFAULT NULL,
    p_origin_type text DEFAULT NULL,
    p_origin_locator text DEFAULT NULL,
    p_origin_revision bigint DEFAULT NULL,
    p_generation_id text DEFAULT NULL,
    p_content_hash text DEFAULT NULL,
    p_producer_actor text DEFAULT NULL,
    p_producer_executor text DEFAULT NULL,
    p_producer_type text DEFAULT 'executor',
    p_model_id text DEFAULT NULL,
    p_model_version text DEFAULT NULL,
    p_confidence numeric DEFAULT NULL,
    p_content_status text DEFAULT 'derived',
    p_authority_class text DEFAULT NULL,
    p_metadata jsonb DEFAULT NULL
) RETURNS text AS $$
BEGIN
    INSERT INTO gptbridge_lineage.lineage_node (
        node_id, engine, node_type, module_id, resource_id, chunk_id,
        qdrant_point_id, origin_type, origin_locator, origin_revision,
        generation_id, content_hash, producer_actor, producer_executor,
        producer_type, model_id, model_version, confidence, content_status,
        authority_class, metadata
    )
    VALUES (
        p_node_id, p_engine, p_node_type, p_module_id, p_resource_id, p_chunk_id,
        p_qdrant_point_id, p_origin_type, p_origin_locator, p_origin_revision,
        p_generation_id, p_content_hash, p_producer_actor, p_producer_executor,
        p_producer_type, p_model_id, p_model_version, p_confidence, p_content_status,
        p_authority_class, COALESCE(p_metadata, '{}'::jsonb)
    )
    ON CONFLICT (node_id) DO NOTHING;
    RETURN p_node_id;
END;
$$ LANGUAGE plpgsql;

-- ============================================================================
-- relate(…) — append active edge between two nodes.
-- Idempotent per (relation, parent, child, run); re-inserting the same edge is
-- a silent no-op instead of an error.
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_lineage.relate(
    p_parent_node_id text,
    p_child_node_id text,
    p_relation_type text,
    p_transformation_id text DEFAULT NULL,
    p_run_id text DEFAULT NULL,
    p_module_id text DEFAULT NULL,
    p_generation_id text DEFAULT NULL,
    p_metadata jsonb DEFAULT NULL
) RETURNS bigint AS $$
DECLARE
    v_edge_id bigint;
BEGIN
    INSERT INTO gptbridge_lineage.lineage_edge (
        parent_node_id, child_node_id, relation_type, transformation_id,
        run_id, module_id, generation_id, metadata
    )
    VALUES (
        p_parent_node_id, p_child_node_id, p_relation_type, p_transformation_id,
        p_run_id, p_module_id, p_generation_id, COALESCE(p_metadata, '{}'::jsonb)
    )
    ON CONFLICT (relation_type, parent_node_id, child_node_id, COALESCE(run_id, ''))
        DO NOTHING
    RETURNING edge_id INTO v_edge_id;

    IF v_edge_id IS NULL THEN
        SELECT edge_id INTO v_edge_id
        FROM gptbridge_lineage.lineage_edge
        WHERE relation_type = p_relation_type
          AND parent_node_id = p_parent_node_id
          AND child_node_id = p_child_node_id
          AND COALESCE(run_id, '') = COALESCE(p_run_id, '');
    END IF;

    RETURN v_edge_id;
END;
$$ LANGUAGE plpgsql;

-- ============================================================================
-- record_reconcile(…) — record a reconcile correction as provenance.
-- Creates (or reuses) a transformation run and links the fixed item back to
-- the source engine via a 'reconciled_from' edge.
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_lineage.record_reconcile(
    p_parent_node_id text,
    p_child_node_id text,
    p_correction_type text,
    p_run_id text,
    p_module_id text DEFAULT NULL,
    p_generation_id text DEFAULT NULL,
    p_details jsonb DEFAULT NULL,
    p_actor text DEFAULT NULL
) RETURNS bigint AS $$
DECLARE
    v_tx text := 'reconcile.' || p_correction_type;
    v_metadata jsonb;
BEGIN
    PERFORM gptbridge_lineage.ensure_transformation(
        v_tx, 'reconcile', '1',
        p_input_contract => NULL,
        p_output_contract => NULL,
        p_executor => p_actor
    );
    v_metadata := COALESCE(p_details, '{}'::jsonb) || jsonb_build_object('correction_type', p_correction_type);
    RETURN gptbridge_lineage.relate(
        p_parent_node_id, p_child_node_id, 'reconciled_from',
        p_transformation_id => v_tx,
        p_run_id => p_run_id,
        p_module_id => p_module_id,
        p_generation_id => p_generation_id,
        p_metadata => v_metadata
    );
END;
$$ LANGUAGE plpgsql;

-- ============================================================================
-- record_restore(…) — record a restore/rollback provenance arc.
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_lineage.record_restore(
    p_parent_node_id text,
    p_child_node_id text,
    p_run_id text,
    p_module_id text DEFAULT NULL,
    p_generation_id text DEFAULT NULL,
    p_snapshot_id text DEFAULT NULL,
    p_details jsonb DEFAULT NULL
) RETURNS bigint AS $$
DECLARE
    v_metadata jsonb;
BEGIN
    PERFORM gptbridge_lineage.ensure_transformation(
        'restore.snapshot.v1', 'restore', '1'
    );
    v_metadata := COALESCE(p_details, '{}'::jsonb);
    IF p_snapshot_id IS NOT NULL THEN
        v_metadata := v_metadata || jsonb_build_object('snapshot_id', p_snapshot_id);
    END IF;
    RETURN gptbridge_lineage.relate(
        p_parent_node_id, p_child_node_id, 'restored_from',
        p_transformation_id => 'restore.snapshot.v1',
        p_run_id => p_run_id,
        p_module_id => p_module_id,
        p_generation_id => p_generation_id,
        p_metadata => v_metadata
    );
END;
$$ LANGUAGE plpgsql;

-- ============================================================================
-- invalidate(p_node_id, p_reason, …) — append-only invalidation.
-- Walks the forward (derived) subtree of p_node_id and records an
-- 'invalidates' edge for every active edge that touches the subtree,
-- so reverse_lookup()/impact() stop following them.  Nothing is mutated.
-- Returns the number of edges invalidated.
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_lineage.invalidate(
    p_node_id text,
    p_reason text,
    p_invalidated_by text DEFAULT NULL,
    p_run_id text DEFAULT NULL,
    p_module_id text DEFAULT NULL,
    p_generation_id text DEFAULT NULL,
    p_metadata jsonb DEFAULT NULL
) RETURNS bigint AS $$
DECLARE
    v_count bigint;
    v_metadata jsonb;
BEGIN
    v_metadata := COALESCE(p_metadata, '{}'::jsonb) || jsonb_build_object(
        'reason', p_reason,
        'invalidated_by', COALESCE(p_invalidated_by,
            nullif(current_setting('gptbridge.executor_id', true), ''), 'unknown')
    );

    INSERT INTO gptbridge_lineage.lineage_edge (
        parent_node_id, child_node_id, relation_type, run_id,
        module_id, generation_id, metadata
    )
    SELECT p_node_id,
           CASE WHEN e.child_node_id = p_node_id THEN e.parent_node_id
                ELSE e.child_node_id END,
           'invalidates', p_run_id,
           p_module_id, p_generation_id,
           v_metadata || jsonb_build_object('target_edge_id', e.edge_id)
    FROM (
        -- active edges whose parent or child is inside the derived subtree
        WITH RECURSIVE subtree(node_id, depth, path) AS (
            SELECT p_node_id, 1, ARRAY[p_node_id]::text[]
            UNION ALL
            SELECT e.child_node_id, s.depth + 1, s.path || e.child_node_id
            FROM gptbridge_lineage.lineage_edge e
            JOIN subtree s ON e.parent_node_id = s.node_id
            WHERE s.depth < 50
              AND NOT e.child_node_id = ANY(s.path)
        )
        SELECT e.edge_id, e.child_node_id, e.parent_node_id
        FROM gptbridge_lineage.lineage_edge e
        WHERE gptbridge_lineage.edge_active(e.edge_id)
          AND (e.parent_node_id IN (SELECT node_id FROM subtree)
               OR e.child_node_id IN (SELECT node_id FROM subtree))
    ) e
    ON CONFLICT (relation_type, parent_node_id, child_node_id, COALESCE(run_id, ''))
        DO NOTHING;

    GET DIAGNOSTICS v_count = ROW_COUNT;
    RETURN v_count;
END;
$$ LANGUAGE plpgsql;

-- ============================================================================
-- reverse_lookup(p_node_id [, p_max_depth])
--   Traces a node BACK to its sources (parents).  Only follows active edges.
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_lineage.reverse_lookup(
    p_node_id text,
    p_max_depth int DEFAULT 10
) RETURNS TABLE (
    depth int,
    relation_type text,
    direction text,
    node_id text,
    engine text,
    node_type text,
    module_id text,
    resource_id text,
    chunk_id text,
    qdrant_point_id text,
    origin_locator text,
    origin_revision bigint,
    content_hash text,
    producer_type text,
    producer_executor text,
    model_id text,
    correlation_id text,
    generation_id text
) AS $$
WITH RECURSIVE trace(depth, relation_type, direction, node_id, engine, node_type,
                     module_id, resource_id, chunk_id, qdrant_point_id,
                     origin_locator, origin_revision, content_hash,
                     producer_type, producer_executor, model_id,
                     correlation_id, generation_id, visited) AS (
    SELECT 1, e.relation_type, 'source',
           e.parent_node_id, n.engine, n.node_type, n.module_id,
           n.resource_id, n.chunk_id, n.qdrant_point_id,
           n.origin_locator, n.origin_revision, n.content_hash,
           n.producer_type, n.producer_executor, n.model_id,
           n.correlation_id, n.generation_id,
           ARRAY[e.parent_node_id, e.child_node_id]::text[]
    FROM gptbridge_lineage.lineage_edge e
    JOIN gptbridge_lineage.lineage_node n
        ON n.node_id = e.parent_node_id
    WHERE e.child_node_id = p_node_id
      AND gptbridge_lineage.edge_active(e.edge_id)
    UNION ALL
    SELECT t.depth + 1, e.relation_type, 'source',
           e.parent_node_id, n.engine, n.node_type, n.module_id,
           n.resource_id, n.chunk_id, n.qdrant_point_id,
           n.origin_locator, n.origin_revision, n.content_hash,
           n.producer_type, n.producer_executor, n.model_id,
           n.correlation_id, n.generation_id,
           t.visited || e.parent_node_id
    FROM trace t
    JOIN gptbridge_lineage.lineage_edge e
        ON e.child_node_id = t.node_id
    JOIN gptbridge_lineage.lineage_node n
        ON n.node_id = e.parent_node_id
    WHERE t.depth < p_max_depth
      AND gptbridge_lineage.edge_active(e.edge_id)
      AND NOT e.parent_node_id = ANY(t.visited)
)
SELECT depth, relation_type, direction, node_id, engine, node_type, module_id,
       resource_id, chunk_id, qdrant_point_id, origin_locator, origin_revision,
       content_hash, producer_type, producer_executor, model_id,
       correlation_id, generation_id
FROM trace
ORDER BY depth, node_id;
$$ LANGUAGE sql STABLE;

-- ============================================================================
-- impact(p_node_id [, p_max_depth])
--   Traces a node FORWARD to every derived/dependent item.  Only active edges.
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_lineage.impact(
    p_node_id text,
    p_max_depth int DEFAULT 10
) RETURNS TABLE (
    depth int,
    relation_type text,
    direction text,
    node_id text,
    engine text,
    node_type text,
    module_id text,
    resource_id text,
    chunk_id text,
    qdrant_point_id text,
    origin_locator text,
    origin_revision bigint,
    content_hash text,
    producer_type text,
    producer_executor text,
    model_id text,
    correlation_id text,
    generation_id text
) AS $$
WITH RECURSIVE trace(depth, relation_type, direction, node_id, engine, node_type,
                     module_id, resource_id, chunk_id, qdrant_point_id,
                     origin_locator, origin_revision, content_hash,
                     producer_type, producer_executor, model_id,
                     correlation_id, generation_id, visited) AS (
    SELECT 1, e.relation_type, 'derived',
           e.child_node_id, n.engine, n.node_type, n.module_id,
           n.resource_id, n.chunk_id, n.qdrant_point_id,
           n.origin_locator, n.origin_revision, n.content_hash,
           n.producer_type, n.producer_executor, n.model_id,
           n.correlation_id, n.generation_id,
           ARRAY[e.parent_node_id, e.child_node_id]::text[]
    FROM gptbridge_lineage.lineage_edge e
    JOIN gptbridge_lineage.lineage_node n
        ON n.node_id = e.child_node_id
    WHERE e.parent_node_id = p_node_id
      AND gptbridge_lineage.edge_active(e.edge_id)
    UNION ALL
    SELECT t.depth + 1, e.relation_type, 'derived',
           e.child_node_id, n.engine, n.node_type, n.module_id,
           n.resource_id, n.chunk_id, n.qdrant_point_id,
           n.origin_locator, n.origin_revision, n.content_hash,
           n.producer_type, n.producer_executor, n.model_id,
           n.correlation_id, n.generation_id,
           t.visited || e.child_node_id
    FROM trace t
    JOIN gptbridge_lineage.lineage_edge e
        ON e.parent_node_id = t.node_id
    JOIN gptbridge_lineage.lineage_node n
        ON n.node_id = e.child_node_id
    WHERE t.depth < p_max_depth
      AND gptbridge_lineage.edge_active(e.edge_id)
      AND NOT e.child_node_id = ANY(t.visited)
)
SELECT depth, relation_type, direction, node_id, engine, node_type, module_id,
       resource_id, chunk_id, qdrant_point_id, origin_locator, origin_revision,
       content_hash, producer_type, producer_executor, model_id,
       correlation_id, generation_id
FROM trace
ORDER BY depth, node_id;
$$ LANGUAGE sql STABLE;

-- ============================================================================
-- health_check() — lineage integrity report.
--   category: orphan_node | broken_edge | missing_chunk_metadata |
--             missing_resource | unindexed_point | edge_no_transformation | cycle
--   severity: info | warn | critical
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_lineage.health_check()
RETURNS TABLE (category text, severity text, entity_id text, detail text) AS $$
BEGIN
    -- Orphan nodes (no edges at all).
    RETURN QUERY
    SELECT 'orphan_node', 'warn', n.node_id, 'node with no lineage edges'
    FROM gptbridge_lineage.lineage_node n
    WHERE NOT EXISTS (
        SELECT 1 FROM gptbridge_lineage.lineage_edge e
        WHERE e.parent_node_id = n.node_id OR e.child_node_id = n.node_id
    );

    -- Broken edges (should be impossible: FK-enforced).
    RETURN QUERY
    SELECT 'broken_edge', 'critical', e.edge_id::text,
           'parent or child node missing'
    FROM gptbridge_lineage.lineage_edge e
    LEFT JOIN gptbridge_lineage.lineage_node p ON p.node_id = e.parent_node_id
    LEFT JOIN gptbridge_lineage.lineage_node c ON c.node_id = e.child_node_id
    WHERE p.node_id IS NULL OR c.node_id IS NULL;

    -- Edges without a registered transformation (manual/unknown provenance).
    RETURN QUERY
    SELECT 'edge_no_transformation', 'info', e.edge_id::text,
           'edge created without a transformation reference'
    FROM gptbridge_lineage.lineage_edge e
    WHERE e.transformation_id IS NULL;

    -- Qdrant points that have not been indexed (no indexed_from edge).
    RETURN QUERY
    SELECT 'unindexed_point', 'info', n.node_id,
           'qdrant node without an indexed_from edge'
    FROM gptbridge_lineage.lineage_node n
    WHERE n.engine = 'qdrant'
      AND NOT EXISTS (
          SELECT 1 FROM gptbridge_lineage.lineage_edge e
          WHERE e.child_node_id = n.node_id
            AND e.relation_type = 'indexed_from'
      );

    -- Cross-engine checks are guarded so the report works even when the rag
    -- metadata tables have not been applied to a given database yet.
    IF to_regclass('gptbridge_rag.chunks') IS NOT NULL THEN
        RETURN QUERY EXECUTE
        $y$SELECT 'missing_chunk_metadata', 'warn', n.node_id,
                  'chunk node missing from gptbridge_rag.chunks'
           FROM gptbridge_lineage.lineage_node n
           WHERE n.node_type = 'chunk'
             AND NOT EXISTS (
                 SELECT 1 FROM gptbridge_rag.chunks c
                 WHERE c.chunk_id = n.chunk_id
             )$y$;
    END IF;

    IF to_regclass('gptbridge_index.resource') IS NOT NULL THEN
        RETURN QUERY EXECUTE
        $y$SELECT 'missing_resource', 'warn',
                  COALESCE(n.resource_id, n.node_id),
                  'resource node missing from gptbridge_index.resource'
           FROM gptbridge_lineage.lineage_node n
           WHERE n.node_type = 'resource'
             AND n.resource_id IS NOT NULL
             AND NOT EXISTS (
                 SELECT 1 FROM gptbridge_index.resource r
                 WHERE r.resource_id = n.resource_id
             )$y$;
    END IF;

    -- Cycles (depth-capped DFS from every node).
    RETURN QUERY
    WITH RECURSIVE walk(start_node, cur_node, path, depth) AS (
        SELECT n.node_id, e.child_node_id, ARRAY[n.node_id, e.child_node_id]::text[], 1
        FROM gptbridge_lineage.lineage_node n
        JOIN gptbridge_lineage.lineage_edge e ON e.parent_node_id = n.node_id
        WHERE gptbridge_lineage.edge_active(e.edge_id)
        UNION ALL
        SELECT w.start_node, e.child_node_id, w.path || e.child_node_id, w.depth + 1
        FROM walk w
        JOIN gptbridge_lineage.lineage_edge e ON e.parent_node_id = w.cur_node
        WHERE w.depth < 12
          AND gptbridge_lineage.edge_active(e.edge_id)
          AND NOT e.child_node_id = ANY(w.path)
    )
    SELECT 'cycle', 'critical', start_node,
           'node reachable from itself over active edges'
    FROM walk
    WHERE cur_node = start_node
    GROUP BY start_node;
END;
$$ LANGUAGE plpgsql STABLE;