-- Apply this schema inside every module-owned PostgreSQL database.
-- Raw and derived files remain in the module's own NTFS directory.
CREATE SCHEMA IF NOT EXISTS module_data;
CREATE SCHEMA IF NOT EXISTS module_state;
CREATE SCHEMA IF NOT EXISTS module_audit;

CREATE TABLE IF NOT EXISTS module_data.resource (
    resource_id text PRIMARY KEY,
    platform_id text NOT NULL,
    module_id text NOT NULL,
    owner_id text NOT NULL,
    data_category text NOT NULL,
    resource_type text NOT NULL,
    resource_label text NOT NULL UNIQUE,
    classification text NOT NULL,
    ntfs_relative_path text,
    content_hash text,
    version bigint NOT NULL DEFAULT 1 CHECK (version > 0),
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS module_data.locator_map (
    locator_id uuid PRIMARY KEY,
    module_id text NOT NULL,
    resource_id text NOT NULL REFERENCES module_data.resource(resource_id),
    ntfs_relative_path text NOT NULL,
    content_hash text,
    version bigint NOT NULL DEFAULT 1,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (module_id, resource_id)
);

CREATE TABLE IF NOT EXISTS module_state.operation (
    operation_id uuid PRIMARY KEY,
    module_id text NOT NULL,
    resource_id text,
    operation_type text NOT NULL,
    status text NOT NULL,
    details jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS module_audit.event (
    event_id uuid PRIMARY KEY,
    actor_id text NOT NULL,
    module_id text NOT NULL,
    resource_id text,
    action text NOT NULL,
    outcome text NOT NULL,
    details jsonb NOT NULL DEFAULT '{}'::jsonb,
    occurred_at timestamptz NOT NULL DEFAULT now()
);

REVOKE ALL ON SCHEMA module_data, module_state, module_audit FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA module_data FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA module_state FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA module_audit FROM PUBLIC;

ALTER TABLE module_data.resource ENABLE ROW LEVEL SECURITY;
ALTER TABLE module_data.resource FORCE ROW LEVEL SECURITY;
ALTER TABLE module_data.locator_map ENABLE ROW LEVEL SECURITY;
ALTER TABLE module_data.locator_map FORCE ROW LEVEL SECURITY;
ALTER TABLE module_state.operation ENABLE ROW LEVEL SECURITY;
ALTER TABLE module_state.operation FORCE ROW LEVEL SECURITY;
ALTER TABLE module_audit.event ENABLE ROW LEVEL SECURITY;
ALTER TABLE module_audit.event FORCE ROW LEVEL SECURITY;

-- Deployment must replace MODULE_ROLE and MODULE_ID before applying this template.
-- No PUBLIC grants or cross-module roles are created here.
