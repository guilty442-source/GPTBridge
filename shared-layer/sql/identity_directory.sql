CREATE SCHEMA IF NOT EXISTS gptbridge_identity;

CREATE TABLE IF NOT EXISTS gptbridge_identity.identity_group (
    group_id            TEXT PRIMARY KEY,
    group_name          TEXT NOT NULL,
    display_name_zh     TEXT NOT NULL,
    group_type          TEXT NOT NULL CHECK (group_type IN ('active', 'sovereign', 'module', 'companion')),
    management_authority TEXT NOT NULL,
    registry_mode       TEXT NOT NULL,
    legacy_identity_compatibility BOOLEAN NOT NULL DEFAULT false,
    aliases_allowed     BOOLEAN NOT NULL DEFAULT false,
    unknown_identity_access TEXT NOT NULL,
    authentication      TEXT NOT NULL,
    active              BOOLEAN NOT NULL DEFAULT true
);

CREATE TABLE IF NOT EXISTS gptbridge_identity.identity (
    identity_id         TEXT PRIMARY KEY,
    identity_code       TEXT NOT NULL UNIQUE,
    identity_name       TEXT NOT NULL,
    display_name_zh     TEXT NOT NULL,
    identity_type       TEXT NOT NULL CHECK (identity_type IN ('sovereign', 'module', 'companion')),
    group_id            TEXT NOT NULL REFERENCES gptbridge_identity.identity_group(group_id),
    actor               TEXT NOT NULL UNIQUE,
    bound_tool_id       TEXT NOT NULL,
    bound_roots         JSONB NOT NULL DEFAULT '[]'::jsonb,
    manifest_required   BOOLEAN NOT NULL DEFAULT false,
    manifest_path_template TEXT NOT NULL DEFAULT '',
    manifest_tool_id_field TEXT NOT NULL DEFAULT '',
    manifest_max_bytes  BIGINT NOT NULL DEFAULT 0,
    authentication      TEXT NOT NULL DEFAULT 'governance-policy-issued-capability-token'
);

CREATE TABLE IF NOT EXISTS gptbridge_identity.identity_required_capability (
    identity_id         TEXT NOT NULL REFERENCES gptbridge_identity.identity(identity_id),
    capability          TEXT NOT NULL,
    PRIMARY KEY (identity_id, capability)
);

CREATE TABLE IF NOT EXISTS gptbridge_identity.identity_manifest_requirement (
    identity_id         TEXT NOT NULL REFERENCES gptbridge_identity.identity(identity_id),
    field_path          TEXT NOT NULL,
    expected_value      TEXT NOT NULL,
    PRIMARY KEY (identity_id, field_path)
);

CREATE TABLE IF NOT EXISTS gptbridge_identity.identity_permission (
    identity_id         TEXT NOT NULL REFERENCES gptbridge_identity.identity(identity_id),
    capability          TEXT NOT NULL,
    PRIMARY KEY (identity_id, capability)
);

REVOKE ALL ON SCHEMA gptbridge_identity FROM PUBLIC;
