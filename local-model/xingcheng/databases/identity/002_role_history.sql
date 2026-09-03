CREATE SCHEMA IF NOT EXISTS role_history;
CREATE TABLE IF NOT EXISTS role_history.personality_version (
    resource_id text NOT NULL REFERENCES role_data.personality(resource_id),
    platform_id text NOT NULL DEFAULT 'local-model-platform', module_id text NOT NULL DEFAULT 'xingcheng',
    owner_id text NOT NULL DEFAULT 'xingcheng', data_category text NOT NULL DEFAULT 'role-setting',
    resource_type text NOT NULL DEFAULT 'personality-version', resource_label text NOT NULL,
    classification text NOT NULL DEFAULT 'private', value jsonb NOT NULL,
    version bigint NOT NULL CHECK (version>0), content_hash text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (resource_id,version)
);
