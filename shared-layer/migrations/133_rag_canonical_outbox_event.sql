-- 133_rag_canonical_outbox_event.sql
-- RAG-08 canonical transactional outbox (gptbridge_rag.outbox_event).
--
-- The canonical RAG outbox authority is declared in
-- core_system.rag.rag_metadata._SAGA_DDL (single DDL authority, per
-- rag/outbox.py); this migration materialises it through the migration
-- chain so least-privilege runtime logins never depend on opportunistic
-- CREATE TABLE from application code.
--
-- Additive and idempotent; no runtime behaviour changes on apply.

CREATE TABLE IF NOT EXISTS gptbridge_rag.outbox_event (
    event_id UUID PRIMARY KEY,
    request_id TEXT NOT NULL,
    operation TEXT NOT NULL CHECK (operation IN (
        'UPSERT_RESOURCE','DELETE_RESOURCE','REINDEX_RESOURCE',
        'RECONCILE_RESOURCE','UPDATE_METADATA',
        'UPSERT','DELETE','REINDEX')),
    module_id TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    source_version BIGINT NOT NULL DEFAULT 0,
    content_hash TEXT NOT NULL DEFAULT '',
    generation_id TEXT NOT NULL DEFAULT '',
    payload JSONB,
    state TEXT NOT NULL DEFAULT 'PENDING' CHECK (state IN (
        'PENDING','PROCESSING','RETRY','SUCCEEDED','DEAD_LETTER')),
    attempt_count INT NOT NULL DEFAULT 0,
    next_retry_at TIMESTAMPTZ,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_outbox_event_state_created
ON gptbridge_rag.outbox_event (state, created_at);

CREATE INDEX IF NOT EXISTS idx_outbox_event_retry
ON gptbridge_rag.outbox_event (next_retry_at) WHERE state = 'RETRY';

CREATE INDEX IF NOT EXISTS idx_outbox_event_generation
ON gptbridge_rag.outbox_event (generation_id, state);

-- Updated-at trigger (mirrors sql_governance/outbox_inbox.py).
CREATE OR REPLACE FUNCTION gptbridge_rag.update_outbox_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_outbox_event_updated_at ON gptbridge_rag.outbox_event;
CREATE TRIGGER trg_outbox_event_updated_at
    BEFORE UPDATE ON gptbridge_rag.outbox_event
    FOR EACH ROW EXECUTE FUNCTION gptbridge_rag.update_outbox_updated_at();

REVOKE ALL ON gptbridge_rag.outbox_event FROM PUBLIC;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'gptbridge_owner') THEN
        EXECUTE 'GRANT ALL ON TABLE gptbridge_rag.outbox_event TO gptbridge_owner';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'gptbridge_runtime') THEN
        EXECUTE 'GRANT SELECT, INSERT, UPDATE ON TABLE gptbridge_rag.outbox_event TO gptbridge_runtime';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'gptbridge_xingcheng_reader') THEN
        EXECUTE 'GRANT SELECT ON TABLE gptbridge_rag.outbox_event TO gptbridge_xingcheng_reader';
    END IF;
END $$;