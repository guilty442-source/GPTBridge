CREATE SCHEMA IF NOT EXISTS cognition;

CREATE TABLE IF NOT EXISTS cognition.model_data (
    resource_id text PRIMARY KEY,
    platform_id text NOT NULL DEFAULT 'local-model-platform',
    module_id text NOT NULL DEFAULT 'xingcheng',
    owner_id text NOT NULL DEFAULT 'xingcheng',
    resource_label text NOT NULL UNIQUE,
    data_type text NOT NULL,
    value jsonb NOT NULL,
    version bigint NOT NULL DEFAULT 1 CHECK (version > 0),
    content_hash text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS cognition.knowledge (
    resource_id text PRIMARY KEY,
    platform_id text NOT NULL DEFAULT 'local-model-platform',
    module_id text NOT NULL DEFAULT 'xingcheng',
    owner_id text NOT NULL DEFAULT 'xingcheng',
    resource_label text NOT NULL UNIQUE,
    knowledge_type text NOT NULL,
    value jsonb NOT NULL,
    classification text NOT NULL DEFAULT 'private',
    version bigint NOT NULL DEFAULT 1 CHECK (version > 0),
    content_hash text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS cognition.model_capability (
    model_id text NOT NULL,
    capability_id text NOT NULL,
    settings jsonb NOT NULL DEFAULT '{}'::jsonb,
    evaluation jsonb NOT NULL DEFAULT '{}'::jsonb,
    version bigint NOT NULL DEFAULT 1 CHECK (version > 0),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (model_id, capability_id)
);

CREATE TABLE IF NOT EXISTS cognition.rag_reference (
    resource_id text NOT NULL REFERENCES cognition.knowledge(resource_id),
    central_resource_id text NOT NULL,
    qdrant_point_id uuid,
    module_id text NOT NULL,
    version bigint NOT NULL DEFAULT 1,
    PRIMARY KEY (resource_id, central_resource_id)
);

REVOKE ALL ON SCHEMA cognition FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA cognition FROM PUBLIC;

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='gptbridge_xingcheng_internal') THEN
        CREATE ROLE gptbridge_xingcheng_internal NOLOGIN;
    END IF;
END $$;
GRANT USAGE ON SCHEMA cognition TO gptbridge_xingcheng_internal;
GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA cognition
    TO gptbridge_xingcheng_internal;
ALTER TABLE cognition.model_data ENABLE ROW LEVEL SECURITY;
ALTER TABLE cognition.model_data FORCE ROW LEVEL SECURITY;
ALTER TABLE cognition.knowledge ENABLE ROW LEVEL SECURITY;
ALTER TABLE cognition.knowledge FORCE ROW LEVEL SECURITY;
ALTER TABLE cognition.model_capability ENABLE ROW LEVEL SECURITY;
ALTER TABLE cognition.model_capability FORCE ROW LEVEL SECURITY;
ALTER TABLE cognition.rag_reference ENABLE ROW LEVEL SECURITY;
ALTER TABLE cognition.rag_reference FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS xingcheng_internal_model_data ON cognition.model_data;
CREATE POLICY xingcheng_internal_model_data ON cognition.model_data FOR ALL
    USING (pg_has_role(current_user,'gptbridge_xingcheng_internal','member'))
    WITH CHECK (pg_has_role(current_user,'gptbridge_xingcheng_internal','member'));
DROP POLICY IF EXISTS xingcheng_internal_knowledge ON cognition.knowledge;
CREATE POLICY xingcheng_internal_knowledge ON cognition.knowledge FOR ALL
    USING (pg_has_role(current_user,'gptbridge_xingcheng_internal','member'))
    WITH CHECK (pg_has_role(current_user,'gptbridge_xingcheng_internal','member'));
DROP POLICY IF EXISTS xingcheng_internal_capability ON cognition.model_capability;
CREATE POLICY xingcheng_internal_capability ON cognition.model_capability FOR ALL
    USING (pg_has_role(current_user,'gptbridge_xingcheng_internal','member'))
    WITH CHECK (pg_has_role(current_user,'gptbridge_xingcheng_internal','member'));
DROP POLICY IF EXISTS xingcheng_internal_rag ON cognition.rag_reference;
CREATE POLICY xingcheng_internal_rag ON cognition.rag_reference FOR ALL
    USING (pg_has_role(current_user,'gptbridge_xingcheng_internal','member'))
    WITH CHECK (pg_has_role(current_user,'gptbridge_xingcheng_internal','member'));
