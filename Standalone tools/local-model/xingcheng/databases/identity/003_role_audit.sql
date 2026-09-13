CREATE SCHEMA IF NOT EXISTS role_audit;
CREATE TABLE IF NOT EXISTS role_audit.event (
    resource_id text PRIMARY KEY, platform_id text NOT NULL DEFAULT 'local-model-platform',
    module_id text NOT NULL DEFAULT 'xingcheng', owner_id text NOT NULL DEFAULT 'xingcheng',
    data_category text NOT NULL DEFAULT 'role-setting-audit', resource_type text NOT NULL DEFAULT 'audit-event',
    resource_label text NOT NULL, classification text NOT NULL DEFAULT 'private', value jsonb NOT NULL,
    version bigint NOT NULL DEFAULT 1 CHECK (version>0), content_hash text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
);
