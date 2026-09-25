-- 147_state_outbox.sql — PostgreSQL takeover: A195 transactional outbox
-- moves off SQLite (state-outbox.sqlite3 retired; PostgreSQL sole
-- structured-data authority).
--
-- Mirrors tasks/state_outbox_store.py's sqlite schema:
--   outbox_entity_revision  per-entity monotonic revision counter
--   outbox_event            durable event log (sequence = BIGSERIAL)
--   outbox_meta             schema_version marker
-- append() semantics: revision bump and event insert in ONE transaction
-- (A195 STATE-CHANGE atomic boundary).

CREATE TABLE IF NOT EXISTS gptbridge_transport.outbox_entity_revision (
    entity_id text    PRIMARY KEY,
    revision  bigint  NOT NULL
);

CREATE TABLE IF NOT EXISTS gptbridge_transport.outbox_event (
    sequence                 bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    entity_id                text NOT NULL,
    entity_type              text NOT NULL,
    operation                text NOT NULL,
    authoritative_revision   bigint NOT NULL,
    previous_revision        bigint NOT NULL,
    changed_field_allowlist  text NOT NULL,
    invalidation_keys        text NOT NULL,
    state_hash               text NOT NULL,
    backend_generation       text NOT NULL,
    release_id               text NOT NULL,
    contract_version         text NOT NULL,
    correlation_id           text NOT NULL,
    committed_at             text NOT NULL,
    recorded_at              text NOT NULL
);

CREATE INDEX IF NOT EXISTS outbox_event_entity_idx
    ON gptbridge_transport.outbox_event (entity_id, sequence);

CREATE TABLE IF NOT EXISTS gptbridge_transport.outbox_meta (
    key   text PRIMARY KEY,
    value text NOT NULL
);

INSERT INTO gptbridge_transport.outbox_meta (key, value)
VALUES ('schema_version', '1')
ON CONFLICT (key) DO NOTHING;

ALTER TABLE gptbridge_transport.outbox_entity_revision ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_transport.outbox_entity_revision FORCE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_transport.outbox_event ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_transport.outbox_event FORCE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_transport.outbox_meta ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_transport.outbox_meta FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS outbox_runtime ON gptbridge_transport.outbox_entity_revision;
CREATE POLICY outbox_runtime ON gptbridge_transport.outbox_entity_revision
    FOR ALL USING (current_user LIKE 'gptbridge_%')
    WITH CHECK (current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS outbox_runtime ON gptbridge_transport.outbox_event;
CREATE POLICY outbox_runtime ON gptbridge_transport.outbox_event
    FOR ALL USING (current_user LIKE 'gptbridge_%')
    WITH CHECK (current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS outbox_runtime ON gptbridge_transport.outbox_meta;
CREATE POLICY outbox_runtime ON gptbridge_transport.outbox_meta
    FOR ALL USING (current_user LIKE 'gptbridge_%')
    WITH CHECK (current_user LIKE 'gptbridge_%');

REVOKE ALL ON gptbridge_transport.outbox_entity_revision FROM PUBLIC;
REVOKE ALL ON gptbridge_transport.outbox_event FROM PUBLIC;
REVOKE ALL ON gptbridge_transport.outbox_meta FROM PUBLIC;
GRANT SELECT, INSERT, UPDATE, DELETE ON gptbridge_transport.outbox_entity_revision
    TO gptbridge_transport_executor;
GRANT SELECT, INSERT, DELETE ON gptbridge_transport.outbox_event
    TO gptbridge_transport_executor;
GRANT SELECT, INSERT ON gptbridge_transport.outbox_meta
    TO gptbridge_transport_executor;
