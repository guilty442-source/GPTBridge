CREATE SCHEMA IF NOT EXISTS role_data;
CREATE TABLE IF NOT EXISTS role_data.personality (
    resource_id text PRIMARY KEY, platform_id text NOT NULL DEFAULT 'local-model-platform',
    module_id text NOT NULL DEFAULT 'xingcheng', owner_id text NOT NULL DEFAULT 'xingcheng',
    data_category text NOT NULL DEFAULT 'role-setting' CHECK (data_category='role-setting'),
    resource_type text NOT NULL DEFAULT 'personality' CHECK (resource_type='personality'),
    resource_label text NOT NULL UNIQUE, classification text NOT NULL DEFAULT 'private',
    value jsonb NOT NULL, version bigint NOT NULL DEFAULT 1 CHECK (version>0),
    content_hash text NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(), CHECK (platform_id='local-model-platform'),
    CHECK (module_id='xingcheng'), CHECK (owner_id='xingcheng')
);
-- Zero rows is correct; at most one personality may be created later.
CREATE UNIQUE INDEX IF NOT EXISTS role_data_single_personality ON role_data.personality ((true));
