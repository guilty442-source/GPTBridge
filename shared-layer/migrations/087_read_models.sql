-- 087_read_models.sql
-- Optimized Read Model (CQRS boundary) — derived, rebuildable, non-authority.
--
-- Purpose:
--   UI / status pages / statistics must NOT scan the authority tables every
--   poll (resource + relation + transport + audit + rag all in one query).
--   This migration creates compact, watermarked read models:
--     * resource_summary          — per (module, resource_type) counts
--     * module_status_summary     — per-module resource + transport health
--     * transport_status_summary  — per-state queue depth + activity
--     * rag_status_summary        — per-module rag metadata + outbox depth
--     * database_status_snapshot  — one-row holistic instance snapshot
--   plus the projection registry and lag classification.
--
-- Design contract (README for future workers):
--   1. gptbridge_index / gptbridge_transport / gptbridge_audit /
--      gptbridge_rag REMAIN the authority.  Everything here is derived.
--   2. Every projection row carries source_generation / source_revision /
--      projection_version / updated_at so read-your-writes and lag are
--      decidable without touching the authority tables.
--   3. Projections are rebuildable: DROP derived → scan authority → rebuild
--      → verify (row_count) → publish.  If a read model cannot be fully
--      rebuilt from authority, it has secretly become an authority itself.
--   4. refresh_projection() atomically builds the new version and switches
--      (readers always see an old-or-new full set, never a partial mix);
--      start_projection_build()/publish_projection_build() expose the same
--      two-phase path for long-running rebuilds.
--   5. get_projection_lag() classifies each projection as CURRENT / STALE /
--      OUTDATED / REBUILD_REQUIRED.  Hot-path reads serve BOUNDED_STALE data;
--      STRONG reads go straight to authority.
--   6. Read models never arbitrate authority conflicts and never gate writes.
--
-- Codex basis:
--   A8/E21  — PostgreSQL: central-structured-official-data (authority stays PG).
--   A10/E10 — explicit-allowlist; deny-by-default.

CREATE SCHEMA IF NOT EXISTS gptbridge_readmodel;

GRANT USAGE ON SCHEMA gptbridge_readmodel TO gptbridge_index_reader;
GRANT USAGE ON SCHEMA gptbridge_readmodel TO gptbridge_xingcheng_reader;
GRANT USAGE ON SCHEMA gptbridge_readmodel TO gptbridge_index_executor;
GRANT USAGE ON SCHEMA gptbridge_readmodel TO gptbridge_transport_executor;

-- ============================================================================
-- Projection registry — one row per read model.
--   state              'active' | 'building'
--   projection_version   the version readers must use (data rows carry it)
--   source_generation    the authority generation this projection reflects
--   source_revision      the authority watermark at last refresh
--   watermark_unit       'revision' | 'epoch_ms' (source_revision unit)
--   row_count            verified at publish time
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_readmodel.projection_version (
    projection_name text PRIMARY KEY,
    source_name text NOT NULL,
    source_generation bigint,
    source_revision bigint NOT NULL DEFAULT 0,
    watermark_unit text NOT NULL DEFAULT 'revision'
        CHECK (watermark_unit IN ('revision', 'epoch_ms')),
    projection_version bigint NOT NULL DEFAULT 1,
    state text NOT NULL DEFAULT 'active'
        CHECK (state IN ('active', 'building')),
    row_count bigint NOT NULL DEFAULT 0,
    refreshed_at timestamptz NOT NULL DEFAULT now(),
    detail jsonb NOT NULL DEFAULT '{}'::jsonb
);

-- ============================================================================
-- resource_summary — per (module, resource_type) aggregates.
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_readmodel.resource_summary (
    module_id text NOT NULL,
    resource_type text NOT NULL,
    resource_count bigint NOT NULL DEFAULT 0,
    total_revisions bigint NOT NULL DEFAULT 0,
    distinct_hash_count bigint NOT NULL DEFAULT 0,
    index_pending_count bigint NOT NULL DEFAULT 0,
    last_activity_at timestamptz,
    source_generation bigint,
    source_revision bigint NOT NULL,
    projection_version bigint NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (projection_version, module_id, resource_type)
);

CREATE INDEX IF NOT EXISTS resource_summary_module_idx
    ON gptbridge_readmodel.resource_summary (module_id, resource_type);

-- ============================================================================
-- module_status_summary — per-module resource + transport health snapshot.
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_readmodel.module_status_summary (
    module_id text NOT NULL,
    resource_count bigint NOT NULL DEFAULT 0,
    transport_queued_count bigint NOT NULL DEFAULT 0,
    transport_pushed_count bigint NOT NULL DEFAULT 0,
    transport_claimed_count bigint NOT NULL DEFAULT 0,
    transport_completed_count bigint NOT NULL DEFAULT 0,
    last_resource_activity timestamptz,
    last_transport_activity timestamptz,
    healthy boolean NOT NULL DEFAULT true,
    source_generation bigint,
    source_revision bigint NOT NULL,
    projection_version bigint NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (projection_version, module_id)
);

-- ============================================================================
-- transport_status_summary — per-state queue depth.
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_readmodel.transport_status_summary (
    state text NOT NULL,
    count bigint NOT NULL DEFAULT 0,
    oldest_created_at timestamptz,
    newest_created_at timestamptz,
    last_activity_at timestamptz,
    source_generation bigint,
    source_revision bigint NOT NULL,
    projection_version bigint NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (projection_version, state)
);

-- ============================================================================
-- rag_status_summary — per-module rag metadata + outbox depth.
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_readmodel.rag_status_summary (
    module_id text NOT NULL,
    chunk_count bigint NOT NULL DEFAULT 0,
    resource_version_count bigint NOT NULL DEFAULT 0,
    index_state_active_count bigint NOT NULL DEFAULT 0,
    outbox_pending_count bigint NOT NULL DEFAULT 0,
    outbox_failed_count bigint NOT NULL DEFAULT 0,
    embedding_model text,
    last_indexed_at timestamptz,
    source_generation bigint,
    source_revision bigint NOT NULL,
    projection_version bigint NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (projection_version, module_id)
);

-- ============================================================================
-- database_status_snapshot — one holistic row, low sensitivity (counts only).
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_readmodel.database_status_snapshot (
    projection_version bigint PRIMARY KEY,
    workspace text,
    pg_version text,
    db_size_bytes bigint,
    schema_count bigint NOT NULL DEFAULT 0,
    table_count bigint NOT NULL DEFAULT 0,
    rls_enforced_table_count bigint NOT NULL DEFAULT 0,
    resource_count bigint NOT NULL DEFAULT 0,
    transport_count bigint NOT NULL DEFAULT 0,
    audit_last_24h_count bigint NOT NULL DEFAULT 0,
    chunk_count bigint NOT NULL DEFAULT 0,
    lineage_node_count bigint NOT NULL DEFAULT 0,
    source_generation bigint,
    source_revision bigint NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- ============================================================================
-- Row level security
-- ============================================================================
ALTER TABLE gptbridge_readmodel.projection_version ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_readmodel.resource_summary ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_readmodel.module_status_summary ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_readmodel.transport_status_summary ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_readmodel.rag_status_summary ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_readmodel.database_status_snapshot ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS rm_registry_read ON gptbridge_readmodel.projection_version;
CREATE POLICY rm_registry_read ON gptbridge_readmodel.projection_version
    FOR SELECT USING (true);
DROP POLICY IF EXISTS rm_registry_write ON gptbridge_readmodel.projection_version;
CREATE POLICY rm_registry_write ON gptbridge_readmodel.projection_version
    FOR ALL USING (current_user LIKE 'gptbridge_%')
    WITH CHECK (current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS rm_summary_read ON gptbridge_readmodel.resource_summary;
CREATE POLICY rm_summary_read ON gptbridge_readmodel.resource_summary
    FOR SELECT USING (gptbridge_security.can_read(module_id));
DROP POLICY IF EXISTS rm_summary_write ON gptbridge_readmodel.resource_summary;
CREATE POLICY rm_summary_write ON gptbridge_readmodel.resource_summary
    FOR ALL USING (gptbridge_security.can_write(module_id))
    WITH CHECK (gptbridge_security.can_write(module_id));

DROP POLICY IF EXISTS rm_module_read ON gptbridge_readmodel.module_status_summary;
CREATE POLICY rm_module_read ON gptbridge_readmodel.module_status_summary
    FOR SELECT USING (gptbridge_security.can_read(module_id));
DROP POLICY IF EXISTS rm_module_write ON gptbridge_readmodel.module_status_summary;
CREATE POLICY rm_module_write ON gptbridge_readmodel.module_status_summary
    FOR ALL USING (gptbridge_security.can_write(module_id))
    WITH CHECK (gptbridge_security.can_write(module_id));

DROP POLICY IF EXISTS rm_transport_read ON gptbridge_readmodel.transport_status_summary;
CREATE POLICY rm_transport_read ON gptbridge_readmodel.transport_status_summary
    FOR SELECT USING (true);
DROP POLICY IF EXISTS rm_transport_write ON gptbridge_readmodel.transport_status_summary;
CREATE POLICY rm_transport_write ON gptbridge_readmodel.transport_status_summary
    FOR ALL USING (current_user LIKE 'gptbridge_%')
    WITH CHECK (current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS rm_rag_read ON gptbridge_readmodel.rag_status_summary;
CREATE POLICY rm_rag_read ON gptbridge_readmodel.rag_status_summary
    FOR SELECT USING (gptbridge_security.can_read(module_id));
DROP POLICY IF EXISTS rm_rag_write ON gptbridge_readmodel.rag_status_summary;
CREATE POLICY rm_rag_write ON gptbridge_readmodel.rag_status_summary
    FOR ALL USING (gptbridge_security.can_write(module_id))
    WITH CHECK (gptbridge_security.can_write(module_id));

DROP POLICY IF EXISTS rm_dbsnap_read ON gptbridge_readmodel.database_status_snapshot;
CREATE POLICY rm_dbsnap_read ON gptbridge_readmodel.database_status_snapshot
    FOR SELECT USING (true);
DROP POLICY IF EXISTS rm_dbsnap_write ON gptbridge_readmodel.database_status_snapshot;
CREATE POLICY rm_dbsnap_write ON gptbridge_readmodel.database_status_snapshot
    FOR ALL USING (current_user LIKE 'gptbridge_%')
    WITH CHECK (current_user LIKE 'gptbridge_%');

REVOKE ALL ON gptbridge_readmodel.projection_version FROM PUBLIC;
REVOKE ALL ON gptbridge_readmodel.resource_summary FROM PUBLIC;
REVOKE ALL ON gptbridge_readmodel.module_status_summary FROM PUBLIC;
REVOKE ALL ON gptbridge_readmodel.transport_status_summary FROM PUBLIC;
REVOKE ALL ON gptbridge_readmodel.rag_status_summary FROM PUBLIC;
REVOKE ALL ON gptbridge_readmodel.database_status_snapshot FROM PUBLIC;

GRANT SELECT ON gptbridge_readmodel.projection_version TO gptbridge_index_reader;
GRANT SELECT ON gptbridge_readmodel.resource_summary TO gptbridge_index_reader;
GRANT SELECT ON gptbridge_readmodel.module_status_summary TO gptbridge_index_reader;
GRANT SELECT ON gptbridge_readmodel.transport_status_summary TO gptbridge_index_reader;
GRANT SELECT ON gptbridge_readmodel.rag_status_summary TO gptbridge_index_reader;
GRANT SELECT ON gptbridge_readmodel.database_status_snapshot TO gptbridge_index_reader;
GRANT SELECT ON gptbridge_readmodel.projection_version TO gptbridge_xingcheng_reader;
GRANT SELECT ON gptbridge_readmodel.resource_summary TO gptbridge_xingcheng_reader;
GRANT SELECT ON gptbridge_readmodel.module_status_summary TO gptbridge_xingcheng_reader;
GRANT SELECT ON gptbridge_readmodel.transport_status_summary TO gptbridge_xingcheng_reader;
GRANT SELECT ON gptbridge_readmodel.rag_status_summary TO gptbridge_xingcheng_reader;
GRANT SELECT ON gptbridge_readmodel.database_status_snapshot TO gptbridge_xingcheng_reader;

GRANT SELECT, INSERT, UPDATE, DELETE ON gptbridge_readmodel.projection_version
    TO gptbridge_index_executor;
GRANT SELECT, INSERT, UPDATE, DELETE ON gptbridge_readmodel.projection_version
    TO gptbridge_transport_executor;
GRANT SELECT, INSERT, UPDATE, DELETE ON gptbridge_readmodel.resource_summary
    TO gptbridge_index_executor;
GRANT SELECT, INSERT, UPDATE, DELETE ON gptbridge_readmodel.module_status_summary
    TO gptbridge_index_executor;
GRANT SELECT, INSERT, UPDATE, DELETE ON gptbridge_readmodel.transport_status_summary
    TO gptbridge_index_executor, gptbridge_transport_executor;
GRANT SELECT, INSERT, UPDATE, DELETE ON gptbridge_readmodel.rag_status_summary
    TO gptbridge_index_executor;
GRANT SELECT, INSERT, UPDATE, DELETE ON gptbridge_readmodel.database_status_snapshot
    TO gptbridge_index_executor;

-- ============================================================================
-- watermark_for(p_projection) — guarded authority watermark per projection.
-- Returns jsonb: {source_revision, source_generation, unit}
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_readmodel.watermark_for(p_projection text)
RETURNS jsonb AS $$
DECLARE
    v_rev bigint := 0;
    v_unit text := 'revision';
BEGIN
    CASE p_projection
        WHEN 'resource_summary' THEN
            IF to_regclass('gptbridge_index.resource') IS NOT NULL THEN
                SELECT COALESCE(MAX(version), 0) INTO v_rev FROM gptbridge_index.resource;
            END IF;
        WHEN 'module_status_summary' THEN
            IF to_regclass('gptbridge_index.resource') IS NOT NULL THEN
                SELECT COALESCE(MAX(version), 0) INTO v_rev FROM gptbridge_index.resource;
            END IF;
        WHEN 'transport_status_summary' THEN
            IF to_regclass('gptbridge_transport.tool_request') IS NOT NULL THEN
                v_unit := 'epoch_ms';
                SELECT COALESCE(MAX(EXTRACT(EPOCH FROM created_at) * 1000)::bigint, 0)
                INTO v_rev FROM gptbridge_transport.tool_request;
            END IF;
        WHEN 'rag_status_summary' THEN
            IF to_regclass('gptbridge_rag.resource_versions') IS NOT NULL THEN
                v_unit := 'epoch_ms';
                SELECT COALESCE(MAX(EXTRACT(EPOCH FROM created_at) * 1000)::bigint, 0)
                INTO v_rev FROM gptbridge_rag.resource_versions;
            ELSIF to_regclass('gptbridge_rag.chunks') IS NOT NULL THEN
                v_unit := 'epoch_ms';
                SELECT COALESCE(MAX(EXTRACT(EPOCH FROM created_at) * 1000)::bigint, 0)
                INTO v_rev FROM gptbridge_rag.chunks;
            END IF;
        WHEN 'database_status_snapshot' THEN
            v_unit := 'epoch_ms';
            v_rev := EXTRACT(EPOCH FROM now())::bigint * 1000;
        ELSE
            RAISE EXCEPTION 'unknown projection: %', p_projection;
    END CASE;
    RETURN jsonb_build_object('source_revision', v_rev, 'source_generation', NULL, 'unit', v_unit);
END;
$$ LANGUAGE plpgsql STABLE;

-- ============================================================================
-- compute_projection_rows(p_projection, p_version) — (re)compute the derived
-- rows for one projection at a given version.  Only READS authority tables;
-- the publish step retires older versions of the same projection.
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_readmodel.compute_projection_rows(
    p_projection text,
    p_version bigint
) RETURNS bigint AS $$
DECLARE
    v_rows bigint := 0;
BEGIN
    CASE p_projection
        WHEN 'resource_summary' THEN
            IF to_regclass('gptbridge_index.resource') IS NULL THEN
                RETURN 0;
            END IF;
            INSERT INTO gptbridge_readmodel.resource_summary (
                module_id, resource_type, resource_count, total_revisions,
                distinct_hash_count, index_pending_count, last_activity_at,
                source_revision, projection_version, updated_at
            )
            SELECT r.module_id, r.resource_type,
                   COUNT(*), COALESCE(SUM(r.version), 0),
                   COUNT(DISTINCT r.content_hash),
                   COUNT(*) FILTER (WHERE r.index_status = 'pending'),
                   MAX(r.updated_at),
                   COALESCE(MAX(r.version), 0), p_version, now()
            FROM gptbridge_index.resource r
            GROUP BY r.module_id, r.resource_type;
            GET DIAGNOSTICS v_rows = ROW_COUNT;

        WHEN 'module_status_summary' THEN
            INSERT INTO gptbridge_readmodel.module_status_summary (
                module_id, resource_count, transport_queued_count,
                transport_pushed_count, transport_claimed_count,
                transport_completed_count, last_resource_activity,
                last_transport_activity, healthy,
                source_revision, projection_version, updated_at
            )
            SELECT base.module_id,
                   COALESCE(m.resource_count, 0),
                   COALESCE(tq.cnt, 0),
                   COALESCE(tp.cnt, 0),
                   COALESCE(tc.cnt, 0),
                   COALESCE(td.cnt, 0),
                   m.last_activity,
                   tr.last_activity,
                   COALESCE(tc.cnt, 0) < 50,
                   COALESCE(m.max_version, 0),
                   p_version, now()
            FROM (
                SELECT module_id FROM gptbridge_index.resource GROUP BY 1
                UNION
                SELECT requester_actor FROM gptbridge_transport.tool_request GROUP BY 1
            ) base
            LEFT JOIN (
                SELECT module_id, COUNT(*) AS resource_count,
                       MAX(updated_at) AS last_activity, MAX(version) AS max_version
                FROM gptbridge_index.resource GROUP BY 1
            ) m ON m.module_id = base.module_id
            LEFT JOIN (
                SELECT requester_actor AS module_id, COUNT(*) AS cnt
                FROM gptbridge_transport.tool_request WHERE status = 'queued' GROUP BY 1
            ) tq ON tq.module_id = base.module_id
            LEFT JOIN (
                SELECT requester_actor AS module_id, COUNT(*) AS cnt
                FROM gptbridge_transport.tool_request WHERE status = 'pushed' GROUP BY 1
            ) tp ON tp.module_id = base.module_id
            LEFT JOIN (
                SELECT requester_actor AS module_id, COUNT(*) AS cnt
                FROM gptbridge_transport.tool_request WHERE status = 'claimed' GROUP BY 1
            ) tc ON tc.module_id = base.module_id
            LEFT JOIN (
                SELECT requester_actor AS module_id, COUNT(*) AS cnt
                FROM gptbridge_transport.tool_request WHERE status = 'completed' GROUP BY 1
            ) td ON td.module_id = base.module_id
            LEFT JOIN (
                SELECT requester_actor AS module_id, MAX(updated_at) AS last_activity
                FROM gptbridge_transport.tool_request GROUP BY 1
            ) tr ON tr.module_id = base.module_id;
            GET DIAGNOSTICS v_rows = ROW_COUNT;

        WHEN 'transport_status_summary' THEN
            IF to_regclass('gptbridge_transport.tool_request') IS NULL THEN
                RETURN 0;
            END IF;
            INSERT INTO gptbridge_readmodel.transport_status_summary (
                state, count, oldest_created_at, newest_created_at,
                last_activity_at, source_revision, projection_version, updated_at
            )
            SELECT t.status, COUNT(*), MIN(t.created_at), MAX(t.created_at),
                   MAX(t.updated_at),
                   COALESCE(MAX(EXTRACT(EPOCH FROM t.created_at) * 1000)::bigint, 0),
                   p_version, now()
            FROM gptbridge_transport.tool_request t
            GROUP BY t.status;
            GET DIAGNOSTICS v_rows = ROW_COUNT;

        WHEN 'rag_status_summary' THEN
            IF to_regclass('gptbridge_rag.resource_versions') IS NULL THEN
                RETURN 0;
            END IF;
            INSERT INTO gptbridge_readmodel.rag_status_summary (
                module_id, chunk_count, resource_version_count,
                index_state_active_count, outbox_pending_count, outbox_failed_count,
                embedding_model, last_indexed_at,
                source_revision, projection_version, updated_at
            )
            SELECT g.module_id,
                   CASE WHEN to_regclass('gptbridge_rag.chunks') IS NOT NULL
                        THEN (SELECT COUNT(*) FROM gptbridge_rag.chunks c2
                              WHERE c2.module_id = g.module_id) ELSE 0 END,
                   CASE WHEN to_regclass('gptbridge_rag.resource_versions') IS NOT NULL
                        THEN (SELECT COUNT(*) FROM gptbridge_rag.resource_versions r2
                              WHERE r2.module_id = g.module_id) ELSE 0 END,
                   CASE WHEN to_regclass('gptbridge_rag.outbox_event') IS NOT NULL
                        THEN (SELECT COUNT(*) FROM gptbridge_rag.index_state i2
                              WHERE i2.module_id = g.module_id AND i2.state = 'ACTIVE') ELSE 0 END,
                   CASE WHEN to_regclass('gptbridge_rag.outbox_event') IS NOT NULL
                        THEN (SELECT COUNT(*) FROM gptbridge_rag.outbox_event o2
                              WHERE o2.module_id = g.module_id AND o2.state = 'PENDING') ELSE 0 END,
                   CASE WHEN to_regclass('gptbridge_rag.outbox_event') IS NOT NULL
                        THEN (SELECT COUNT(*) FROM gptbridge_rag.outbox_event o3
                              WHERE o3.module_id = g.module_id
                                AND o3.state IN ('FAILED', 'DEAD_LETTER')) ELSE 0 END,
                   rv.embedding_model,
                   rv.last_indexed_at,
                   COALESCE(MAX(EXTRACT(EPOCH FROM rv.last_indexed_at) * 1000)::bigint, 0),
                   p_version, now()
            FROM (
                SELECT DISTINCT module_id FROM gptbridge_rag.resource_versions
            ) g
            LEFT JOIN (
                SELECT module_id, MAX(embedding_model) AS embedding_model,
                       MAX(created_at) AS last_indexed_at
                FROM gptbridge_rag.resource_versions GROUP BY 1
            ) rv ON rv.module_id = g.module_id
            GROUP BY g.module_id, rv.embedding_model, rv.last_indexed_at;
            GET DIAGNOSTICS v_rows = ROW_COUNT;

        WHEN 'database_status_snapshot' THEN
            INSERT INTO gptbridge_readmodel.database_status_snapshot (
                projection_version, workspace, pg_version, db_size_bytes,
                schema_count, table_count, rls_enforced_table_count,
                resource_count, transport_count, audit_last_24h_count,
                chunk_count, lineage_node_count,
                source_revision, updated_at
            )
            SELECT p_version,
                   current_setting('cluster_name', true),
                   version(),
                   gptbridge_readmodel.safe_db_size_bytes(),
                   (SELECT COUNT(*) FROM information_schema.schemata
                    WHERE schema_name NOT IN ('pg_catalog','information_schema','pg_toast')),
                   (SELECT COUNT(*) FROM pg_catalog.pg_tables
                    WHERE schemaname NOT IN ('pg_catalog','information_schema','pg_toast')),
                   (SELECT COUNT(*) FROM pg_catalog.pg_class c
                    JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
                    WHERE c.relkind = 'r' AND c.relrowsecurity = true
                      AND n.nspname NOT IN ('pg_catalog','information_schema')),
                   COALESCE((SELECT COUNT(*) FROM gptbridge_index.resource), 0),
                   COALESCE((SELECT COUNT(*) FROM gptbridge_transport.tool_request), 0),
                   COALESCE((SELECT COUNT(*) FROM gptbridge_audit.event
                             WHERE occurred_at > now() - interval '24 hours'), 0),
                   CASE WHEN to_regclass('gptbridge_rag.chunks') IS NOT NULL
                        THEN (SELECT COUNT(*) FROM gptbridge_rag.chunks) ELSE 0 END,
                   CASE WHEN to_regclass('gptbridge_lineage.lineage_node') IS NOT NULL
                        THEN (SELECT COUNT(*) FROM gptbridge_lineage.lineage_node) ELSE 0 END,
                   EXTRACT(EPOCH FROM now())::bigint * 1000,
                   now();
            GET DIAGNOSTICS v_rows = ROW_COUNT;
        ELSE
            RAISE EXCEPTION 'unknown projection: %', p_projection;
    END CASE;
    RETURN v_rows;
END;
$$ LANGUAGE plpgsql;

-- safe_db_size_bytes() — tolerant of missing privileges (pg_database_size).
CREATE OR REPLACE FUNCTION gptbridge_readmodel.safe_db_size_bytes()
RETURNS bigint AS $$
DECLARE
    v_size bigint;
BEGIN
    SELECT pg_database_size(current_database()) INTO v_size;
    RETURN v_size;
EXCEPTION WHEN OTHERS THEN
    RETURN NULL;
END;
$$ LANGUAGE plpgsql STABLE;

-- ============================================================================
-- start_projection_build(p_projection) — reserve the next (building) version.
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_readmodel.start_projection_build(
    p_projection text
) RETURNS bigint AS $$
DECLARE
    v_version bigint;
    v_water jsonb;
BEGIN
    v_water := gptbridge_readmodel.watermark_for(p_projection);
    SELECT COALESCE(MAX(projection_version), 0) + 1 INTO v_version
    FROM gptbridge_readmodel.projection_version
    WHERE projection_name = p_projection;
    IF v_version IS NULL THEN
        v_version := 1;
    END IF;

    INSERT INTO gptbridge_readmodel.projection_version (
        projection_name, source_name, source_revision, watermark_unit,
        projection_version, state
    )
    VALUES (
        p_projection, 'authority',
        (v_water ->> 'source_revision')::bigint,
        v_water ->> 'unit',
        v_version, 'building'
    )
    ON CONFLICT (projection_name) DO UPDATE SET
        state = 'building',
        projection_version = EXCLUDED.projection_version,
        source_revision = EXCLUDED.source_revision,
        watermark_unit = EXCLUDED.watermark_unit,
        refreshed_at = now();
    RETURN v_version;
END;
$$ LANGUAGE plpgsql;

-- ============================================================================
-- publish_projection_build(p_projection, p_build_version) — verify, switch the
-- active version, retire all other rows, and publish the pg_notify hint.
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_readmodel.publish_projection_build(
    p_projection text,
    p_build_version bigint
) RETURNS bigint AS $$
DECLARE
    v_rows bigint;
    v_detail jsonb;
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM gptbridge_readmodel.projection_version
        WHERE projection_name = p_projection
          AND projection_version = p_build_version
          AND state = 'building'
    ) THEN
        RAISE EXCEPTION 'projection % has no building version %', p_projection, p_build_version;
    END IF;

    CASE p_projection
        WHEN 'resource_summary' THEN
            SELECT COUNT(*), COALESCE(jsonb_object_agg(module_id || ':' || resource_type, resource_count), '{}'::jsonb)
            INTO v_rows, v_detail FROM gptbridge_readmodel.resource_summary
            WHERE projection_version = p_build_version;
            DELETE FROM gptbridge_readmodel.resource_summary
            WHERE projection_version <> p_build_version;
        WHEN 'module_status_summary' THEN
            SELECT COUNT(*), COALESCE(jsonb_object_agg(module_id, resource_count), '{}'::jsonb)
            INTO v_rows, v_detail FROM gptbridge_readmodel.module_status_summary
            WHERE projection_version = p_build_version;
            DELETE FROM gptbridge_readmodel.module_status_summary
            WHERE projection_version <> p_build_version;
        WHEN 'transport_status_summary' THEN
            SELECT COUNT(*), COALESCE(jsonb_object_agg(state, count), '{}'::jsonb)
            INTO v_rows, v_detail FROM gptbridge_readmodel.transport_status_summary
            WHERE projection_version = p_build_version;
            DELETE FROM gptbridge_readmodel.transport_status_summary
            WHERE projection_version <> p_build_version;
        WHEN 'rag_status_summary' THEN
            SELECT COUNT(*), COALESCE(jsonb_object_agg(module_id, chunk_count), '{}'::jsonb)
            INTO v_rows, v_detail FROM gptbridge_readmodel.rag_status_summary
            WHERE projection_version = p_build_version;
            DELETE FROM gptbridge_readmodel.rag_status_summary
            WHERE projection_version <> p_build_version;
        WHEN 'database_status_snapshot' THEN
            SELECT COUNT(*) INTO v_rows FROM gptbridge_readmodel.database_status_snapshot
            WHERE projection_version = p_build_version;
            v_detail := '{"snapshot": 1}'::jsonb;
            DELETE FROM gptbridge_readmodel.database_status_snapshot
            WHERE projection_version <> p_build_version;
        ELSE
            RAISE EXCEPTION 'unknown projection: %', p_projection;
    END CASE;

    UPDATE gptbridge_readmodel.projection_version
    SET state = 'active',
        row_count = COALESCE(v_rows, 0),
        detail = COALESCE(v_detail, '{}'::jsonb),
        refreshed_at = now()
    WHERE projection_name = p_projection
      AND projection_version = p_build_version;

    PERFORM pg_notify('gptbridge_readmodel.change', p_projection || ':' || p_build_version);
    RETURN p_build_version;
END;
$$ LANGUAGE plpgsql;

-- ============================================================================
-- refresh_projection(p_projection) — one-call build + verify + switch.
-- Returns the published projection version.
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_readmodel.refresh_projection(
    p_projection text
) RETURNS bigint AS $$
DECLARE
    v_version bigint;
    v_water jsonb;
BEGIN
    v_version := gptbridge_readmodel.start_projection_build(p_projection);
    PERFORM gptbridge_readmodel.compute_projection_rows(p_projection, v_version);
    v_water := gptbridge_readmodel.watermark_for(p_projection);
    UPDATE gptbridge_readmodel.projection_version
    SET source_revision = (v_water ->> 'source_revision')::bigint,
        watermark_unit = v_water ->> 'unit'
    WHERE projection_name = p_projection
      AND projection_version = v_version;
    RETURN gptbridge_readmodel.publish_projection_build(p_projection, v_version);
END;
$$ LANGUAGE plpgsql;

-- ============================================================================
-- drop_projection(p_projection) — DROP derived state (rebuildability contract).
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_readmodel.drop_projection(
    p_projection text
) RETURNS bigint AS $$
DECLARE
    v_left bigint;
BEGIN
    CASE p_projection
        WHEN 'resource_summary' THEN
            DELETE FROM gptbridge_readmodel.resource_summary;
        WHEN 'module_status_summary' THEN
            DELETE FROM gptbridge_readmodel.module_status_summary;
        WHEN 'transport_status_summary' THEN
            DELETE FROM gptbridge_readmodel.transport_status_summary;
        WHEN 'rag_status_summary' THEN
            DELETE FROM gptbridge_readmodel.rag_status_summary;
        WHEN 'database_status_snapshot' THEN
            DELETE FROM gptbridge_readmodel.database_status_snapshot;
        ELSE
            RAISE EXCEPTION 'unknown projection: %', p_projection;
    END CASE;
    DELETE FROM gptbridge_readmodel.projection_version
    WHERE projection_name = p_projection;
    v_left := 0;
    RETURN v_left;
END;
$$ LANGUAGE plpgsql;

-- ============================================================================
-- get_projection_lag(p_projection) — lag classification.
-- Returns: projection_name, projection_revision, authority_revision,
--          revision_lag, lag_seconds, watermark_unit, status, refreshed_at
-- status: CURRENT | STALE | OUTDATED | REBUILD_REQUIRED
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_readmodel.get_projection_lag(
    p_projection text DEFAULT NULL
) RETURNS TABLE (
    projection_name text,
    projection_revision bigint,
    authority_revision bigint,
    revision_lag bigint,
    lag_seconds double precision,
    watermark_unit text,
    status text,
    refreshed_at timestamptz
) AS $$
DECLARE
    v_name text;
    v_water jsonb;
    v_rev bigint;
    v_unit text;
    v_eff_lag bigint;
    v_status text;
    v_pv bigint;
    v_psource bigint;
    v_pstate text;
    v_pcount bigint;
    v_prefreshed timestamptz;
    v_names text[];
BEGIN
    IF p_projection IS NULL THEN
        v_names := ARRAY['resource_summary','module_status_summary',
                         'transport_status_summary','rag_status_summary',
                         'database_status_snapshot'];
    ELSE
        v_names := ARRAY[p_projection];
    END IF;

    FOREACH v_name IN ARRAY v_names LOOP
        v_water := gptbridge_readmodel.watermark_for(v_name);
        v_rev := (v_water ->> 'source_revision')::bigint;
        v_unit := v_water ->> 'unit';

        SELECT projection_version, source_revision, state, row_count, refreshed_at
        INTO v_pv, v_psource, v_pstate, v_pcount, v_prefreshed
        FROM gptbridge_readmodel.projection_version
        WHERE projection_name = v_name;

        projection_name := v_name;
        authority_revision := v_rev;
        watermark_unit := v_unit;

        IF v_pv IS NULL OR v_pstate <> 'active' OR COALESCE(v_pcount, 0) = 0 THEN
            status := 'REBUILD_REQUIRED';
            projection_revision := -1;
            revision_lag := NULL;
            lag_seconds := NULL;
            refreshed_at := NULL;
        ELSE
            projection_revision := v_psource;
            v_eff_lag := GREATEST(v_rev - v_psource, 0);
            IF v_unit = 'epoch_ms' THEN
                v_eff_lag := (v_eff_lag / 1000)::bigint;
            END IF;
            revision_lag := v_eff_lag;
            lag_seconds := EXTRACT(EPOCH FROM (now() - v_prefreshed));
            refreshed_at := v_prefreshed;
            IF v_eff_lag = 0 AND lag_seconds < 30 THEN
                v_status := 'CURRENT';
            ELSIF lag_seconds < 300 THEN
                v_status := 'STALE';
            ELSIF lag_seconds < 3600 THEN
                v_status := 'OUTDATED';
            ELSE
                v_status := 'REBUILD_REQUIRED';
            END IF;
            status := v_status;
        END IF;

        RETURN NEXT;
        v_status := NULL;
        v_pv := NULL;
    END LOOP;
    RETURN;
END;
$$ LANGUAGE plpgsql STABLE;