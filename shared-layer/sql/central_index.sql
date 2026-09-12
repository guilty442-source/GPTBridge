-- central_index.sql — PostgreSQL central structured official data + shared transport + audit.
--
-- Codex basis:
--   A8/E21  — PostgreSQL: central-structured-official-data + shared-transport + audit;
--              FORBID: sqlite-as-central-official-or-shared-audit.
--   A46/E32 — Audit: mandatory-ledger; write=governed-executor; store=data-governance-sub-sovereign-declared.
--   A10/E10 — Authorization: explicit-allowlist; deny-by-default; fail-closed.
--   A52/E38 — RAG: gptbridge_rag schema stores Qdrant point/collection *metadata* only;
--              Qdrant remains the canonical semantic index (local-owned, local-only).
--              This schema MUST NOT store vector payloads — that would substitute PostgreSQL
--              for Qdrant's canonical role (A8 FORBID: role-substitution).
--   A49/E35 — Formal-tools: implementation-dependencies=approved-inventory-not-role-authority.
--
-- Role boundary:
--   gptbridge_index    — central resource registry (structured metadata).
--   gptbridge_rag      — RAG chunk/index-state metadata pointing to Qdrant canonical vectors.
--   gptbridge_transport— shared tool-request transport channel.
--   gptbridge_audit    — central shared audit ledger (A46).  Module-private audit (SQLite)
--                        is bounded operational state only, NEVER a substitute for this schema.
--   gptbridge_security — principal/scope definitions driving RLS (A10 deny-by-default).

CREATE SCHEMA IF NOT EXISTS gptbridge_index;
CREATE SCHEMA IF NOT EXISTS gptbridge_rag;
CREATE SCHEMA IF NOT EXISTS gptbridge_transport;
CREATE SCHEMA IF NOT EXISTS gptbridge_audit;
CREATE SCHEMA IF NOT EXISTS gptbridge_security;

CREATE TABLE IF NOT EXISTS gptbridge_security.principal (
    role_name name PRIMARY KEY,
    module_id text,
    global_read boolean NOT NULL DEFAULT false,
    transport_execute boolean NOT NULL DEFAULT false,
    audit_write boolean NOT NULL DEFAULT false
);

CREATE TABLE IF NOT EXISTS gptbridge_security.principal_scope (
    role_name name NOT NULL REFERENCES gptbridge_security.principal(role_name),
    module_id text NOT NULL,
    can_read boolean NOT NULL DEFAULT false,
    can_write boolean NOT NULL DEFAULT false,
    PRIMARY KEY (role_name, module_id)
);

CREATE OR REPLACE FUNCTION gptbridge_security.can_read(target_module text)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog AS $$
    SELECT EXISTS (
        SELECT 1 FROM gptbridge_security.principal AS principal
        LEFT JOIN gptbridge_security.principal_scope AS scope
          ON scope.role_name = principal.role_name AND scope.module_id = target_module
        WHERE pg_has_role(session_user, principal.role_name, 'member')
          AND (principal.global_read OR scope.can_read)
    );
$$;

CREATE OR REPLACE FUNCTION gptbridge_security.can_write(target_module text)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog AS $$
    SELECT EXISTS (
        SELECT 1 FROM gptbridge_security.principal AS principal
        JOIN gptbridge_security.principal_scope AS scope
          ON scope.role_name = principal.role_name AND scope.module_id = target_module
        WHERE pg_has_role(session_user, principal.role_name, 'member')
          AND scope.can_write
    );
$$;

CREATE OR REPLACE FUNCTION gptbridge_security.can_write_resource(
    target_module text, target_classification text
)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog AS $$
    SELECT gptbridge_security.can_write(target_module)
       AND NOT (
           current_setting('gptbridge.actor_kind', true) = 'xingcheng'
           AND target_classification IN (
               'permission', 'permission-file', 'permission-directory', 'governance-rule'
           )
       );
$$;

CREATE TABLE IF NOT EXISTS gptbridge_index.resource (
    resource_id text PRIMARY KEY,
    platform_id text NOT NULL,
    module_id text NOT NULL,
    owner_id text NOT NULL,
    data_category text NOT NULL,
    resource_type text NOT NULL,
    resource_label text NOT NULL UNIQUE,
    classification text NOT NULL,
    locator_id uuid NOT NULL UNIQUE,
    content_hash text,
    version bigint NOT NULL DEFAULT 1 CHECK (version > 0),
    index_status text NOT NULL DEFAULT 'pending',
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (platform_id, module_id, resource_id)
);

CREATE INDEX IF NOT EXISTS resource_module_category_idx
    ON gptbridge_index.resource (module_id, data_category, resource_type);
CREATE INDEX IF NOT EXISTS resource_status_idx
    ON gptbridge_index.resource (index_status, updated_at);
CREATE INDEX IF NOT EXISTS resource_metadata_idx
    ON gptbridge_index.resource USING gin (metadata);

CREATE TABLE IF NOT EXISTS gptbridge_rag.chunk (
    chunk_id text PRIMARY KEY,
    resource_id text NOT NULL REFERENCES gptbridge_index.resource(resource_id),
    module_id text NOT NULL,
    sequence integer NOT NULL CHECK (sequence > 0),
    character_start integer NOT NULL CHECK (character_start >= 0),
    character_end integer NOT NULL CHECK (character_end >= character_start),
    qdrant_point_id uuid NOT NULL UNIQUE,
    embedding_model text NOT NULL,
    locator_fragment text NOT NULL,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (resource_id, sequence)
);

CREATE INDEX IF NOT EXISTS rag_chunk_module_idx
    ON gptbridge_rag.chunk (module_id, resource_id, sequence);

CREATE TABLE IF NOT EXISTS gptbridge_rag.index_state (
    resource_id text PRIMARY KEY REFERENCES gptbridge_index.resource(resource_id),
    module_id text NOT NULL,
    embedding_model text NOT NULL,
    qdrant_collection text NOT NULL,
    chunk_count integer NOT NULL DEFAULT 0,
    status text NOT NULL,
    version bigint NOT NULL DEFAULT 1,
    indexed_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS gptbridge_transport.tool_request (
    channel_id text NOT NULL,
    request_id text NOT NULL,
    requester_actor text NOT NULL,
    target_tool_id text NOT NULL,
    payload jsonb NOT NULL,
    status text NOT NULL,
    response jsonb,
    progress jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (channel_id, request_id)
);

CREATE INDEX IF NOT EXISTS tool_request_claim_idx
    ON gptbridge_transport.tool_request (channel_id, target_tool_id, status, created_at);

CREATE TABLE IF NOT EXISTS gptbridge_index.resource_relation (
    source_resource_id text NOT NULL REFERENCES gptbridge_index.resource(resource_id),
    target_resource_id text NOT NULL REFERENCES gptbridge_index.resource(resource_id),
    relation_type text NOT NULL,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (source_resource_id, target_resource_id, relation_type)
);

CREATE TABLE IF NOT EXISTS gptbridge_audit.event (
    event_id uuid PRIMARY KEY,
    actor_id text NOT NULL,
    module_id text NOT NULL,
    resource_id text,
    action text NOT NULL,
    outcome text NOT NULL,
    decision_id text,
    details jsonb NOT NULL DEFAULT '{}'::jsonb,
    occurred_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE gptbridge_index.resource ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.resource FORCE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.resource_relation ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.resource_relation FORCE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_rag.chunk ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_rag.chunk FORCE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_rag.index_state ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_rag.index_state FORCE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_transport.tool_request ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_transport.tool_request FORCE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_audit.event ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_audit.event FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS resource_read ON gptbridge_index.resource;
CREATE POLICY resource_read ON gptbridge_index.resource FOR SELECT
    USING (gptbridge_security.can_read(module_id));
DROP POLICY IF EXISTS resource_write ON gptbridge_index.resource;
CREATE POLICY resource_write ON gptbridge_index.resource FOR ALL
    USING (gptbridge_security.can_write_resource(module_id, classification))
    WITH CHECK (gptbridge_security.can_write_resource(module_id, classification));

DROP POLICY IF EXISTS relation_read ON gptbridge_index.resource_relation;
CREATE POLICY relation_read ON gptbridge_index.resource_relation FOR SELECT
    USING (
        EXISTS (SELECT 1 FROM gptbridge_index.resource AS source
                WHERE source.resource_id=source_resource_id
                  AND gptbridge_security.can_read(source.module_id))
        AND EXISTS (SELECT 1 FROM gptbridge_index.resource AS target
                    WHERE target.resource_id=target_resource_id
                      AND gptbridge_security.can_read(target.module_id))
    );
DROP POLICY IF EXISTS relation_write ON gptbridge_index.resource_relation;
CREATE POLICY relation_write ON gptbridge_index.resource_relation FOR ALL
    USING (
        EXISTS (SELECT 1 FROM gptbridge_index.resource AS source
                WHERE source.resource_id=source_resource_id
                  AND gptbridge_security.can_write(source.module_id))
        AND EXISTS (SELECT 1 FROM gptbridge_index.resource AS target
                    WHERE target.resource_id=target_resource_id
                      AND gptbridge_security.can_write(target.module_id))
    ) WITH CHECK (
        EXISTS (SELECT 1 FROM gptbridge_index.resource AS source
                WHERE source.resource_id=source_resource_id
                  AND gptbridge_security.can_write(source.module_id))
        AND EXISTS (SELECT 1 FROM gptbridge_index.resource AS target
                    WHERE target.resource_id=target_resource_id
                      AND gptbridge_security.can_write(target.module_id))
    );

DROP POLICY IF EXISTS rag_chunk_read ON gptbridge_rag.chunk;
CREATE POLICY rag_chunk_read ON gptbridge_rag.chunk FOR SELECT
    USING (gptbridge_security.can_read(module_id));
DROP POLICY IF EXISTS rag_chunk_write ON gptbridge_rag.chunk;
CREATE POLICY rag_chunk_write ON gptbridge_rag.chunk FOR ALL
    USING (gptbridge_security.can_write(module_id))
    WITH CHECK (gptbridge_security.can_write(module_id));
DROP POLICY IF EXISTS rag_state_read ON gptbridge_rag.index_state;
CREATE POLICY rag_state_read ON gptbridge_rag.index_state FOR SELECT
    USING (gptbridge_security.can_read(module_id));
DROP POLICY IF EXISTS rag_state_write ON gptbridge_rag.index_state;
CREATE POLICY rag_state_write ON gptbridge_rag.index_state FOR ALL
    USING (gptbridge_security.can_write(module_id))
    WITH CHECK (gptbridge_security.can_write(module_id));

DROP POLICY IF EXISTS transport_executor_only ON gptbridge_transport.tool_request;
CREATE POLICY transport_executor_only ON gptbridge_transport.tool_request FOR ALL
    USING (EXISTS (
        SELECT 1 FROM gptbridge_security.principal
        WHERE pg_has_role(current_user, role_name, 'member') AND transport_execute
    )) WITH CHECK (EXISTS (
        SELECT 1 FROM gptbridge_security.principal
        WHERE pg_has_role(current_user, role_name, 'member') AND transport_execute
    ));

DROP POLICY IF EXISTS audit_read ON gptbridge_audit.event;
CREATE POLICY audit_read ON gptbridge_audit.event FOR SELECT
    USING (gptbridge_security.can_read(module_id));
DROP POLICY IF EXISTS audit_insert ON gptbridge_audit.event;
CREATE POLICY audit_insert ON gptbridge_audit.event FOR INSERT
    WITH CHECK (EXISTS (
        SELECT 1 FROM gptbridge_security.principal
        WHERE pg_has_role(current_user, role_name, 'member') AND audit_write
    ));

REVOKE ALL ON SCHEMA gptbridge_index FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA gptbridge_index FROM PUBLIC;
REVOKE ALL ON SCHEMA gptbridge_rag FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA gptbridge_rag FROM PUBLIC;
REVOKE ALL ON SCHEMA gptbridge_transport FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA gptbridge_transport FROM PUBLIC;
REVOKE ALL ON SCHEMA gptbridge_audit FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA gptbridge_audit FROM PUBLIC;
REVOKE ALL ON SCHEMA gptbridge_security FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA gptbridge_security FROM PUBLIC;

-- Runtime deployment creates these NOLOGIN group roles and grants membership to
-- authenticated services. Models never receive PostgreSQL credentials.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'gptbridge_index_reader') THEN
        CREATE ROLE gptbridge_index_reader NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'gptbridge_index_executor') THEN
        CREATE ROLE gptbridge_index_executor NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'gptbridge_xingcheng_reader') THEN
        CREATE ROLE gptbridge_xingcheng_reader NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'gptbridge_transport_executor') THEN
        CREATE ROLE gptbridge_transport_executor NOLOGIN;
    END IF;
END
$$;

INSERT INTO gptbridge_security.principal (role_name, module_id, global_read)
VALUES ('gptbridge_xingcheng_reader', 'xingcheng', true)
ON CONFLICT (role_name) DO UPDATE SET module_id='xingcheng', global_read=true;
INSERT INTO gptbridge_security.principal (role_name, transport_execute, audit_write)
VALUES ('gptbridge_transport_executor', true, true)
ON CONFLICT (role_name) DO UPDATE SET transport_execute=true, audit_write=true;

GRANT USAGE ON SCHEMA gptbridge_index TO gptbridge_index_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA gptbridge_index TO gptbridge_index_reader;
GRANT USAGE ON SCHEMA gptbridge_rag TO gptbridge_index_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA gptbridge_rag TO gptbridge_index_reader;
GRANT USAGE ON SCHEMA gptbridge_audit TO gptbridge_index_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA gptbridge_audit TO gptbridge_index_reader;
GRANT USAGE ON SCHEMA gptbridge_index, gptbridge_rag, gptbridge_audit
    TO gptbridge_xingcheng_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA gptbridge_index
    TO gptbridge_xingcheng_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA gptbridge_rag
    TO gptbridge_xingcheng_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA gptbridge_audit
    TO gptbridge_xingcheng_reader;
GRANT USAGE ON SCHEMA gptbridge_index TO gptbridge_index_executor;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA gptbridge_index
    TO gptbridge_index_executor;
GRANT USAGE ON SCHEMA gptbridge_rag TO gptbridge_index_executor;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA gptbridge_rag
    TO gptbridge_index_executor;
GRANT USAGE ON SCHEMA gptbridge_transport, gptbridge_audit
    TO gptbridge_index_executor;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA gptbridge_transport
    TO gptbridge_index_executor;
GRANT SELECT, INSERT ON ALL TABLES IN SCHEMA gptbridge_audit
    TO gptbridge_index_executor;
GRANT USAGE ON SCHEMA gptbridge_transport TO gptbridge_transport_executor;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA gptbridge_transport
    TO gptbridge_transport_executor;
