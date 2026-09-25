-- 146_governance_nonce.sql — PostgreSQL takeover: governance anti-replay
-- nonce store moves off SQLite (A348/A610: PostgreSQL sole structured-data
-- authority; nonce_store_engine=sqlite retired).
--
-- Single-use nonce ledger for GovernanceAuthenticationService.  The consume
-- path is one atomic INSERT ... ON CONFLICT DO NOTHING inside a transaction
-- with an expiry sweep — same semantics as the retired sqlite store, now in
-- the central index so replay protection holds across processes and hosts.

CREATE TABLE IF NOT EXISTS gptbridge_transport.governance_used_nonces (
    namespace   text    NOT NULL,
    actor       text    NOT NULL,
    nonce       text    NOT NULL,
    expires_at  bigint  NOT NULL,
    consumed_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (namespace, actor, nonce)
);

CREATE INDEX IF NOT EXISTS governance_used_nonces_expiry_idx
    ON gptbridge_transport.governance_used_nonces (expires_at);

ALTER TABLE gptbridge_transport.governance_used_nonces ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_transport.governance_used_nonces FORCE ROW LEVEL SECURITY;

-- Nonce rows are governance-internal: only governed gptbridge_* runtime
-- roles may read or write them; no tool/module reader access.
DROP POLICY IF EXISTS governance_used_nonces_runtime ON gptbridge_transport.governance_used_nonces;
CREATE POLICY governance_used_nonces_runtime
    ON gptbridge_transport.governance_used_nonces
    FOR ALL
    USING (current_user LIKE 'gptbridge_%')
    WITH CHECK (current_user LIKE 'gptbridge_%');

REVOKE ALL ON gptbridge_transport.governance_used_nonces FROM PUBLIC;
GRANT SELECT, INSERT, DELETE ON gptbridge_transport.governance_used_nonces
    TO gptbridge_index_executor;
GRANT SELECT, INSERT, DELETE ON gptbridge_transport.governance_used_nonces
    TO gptbridge_transport_executor;
