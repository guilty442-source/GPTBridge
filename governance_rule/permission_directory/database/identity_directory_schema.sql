
CREATE TABLE IF NOT EXISTS identity_group (
    group_id            TEXT PRIMARY KEY,
    group_name          TEXT NOT NULL,
    display_name_zh     TEXT NOT NULL,
    group_type          TEXT NOT NULL,
    management_authority TEXT NOT NULL,
    registry_mode       TEXT NOT NULL,
    legacy_identity_compatibility INTEGER NOT NULL DEFAULT 0,
    aliases_allowed     INTEGER NOT NULL DEFAULT 0,
    unknown_identity_access TEXT NOT NULL,
    authentication      TEXT NOT NULL,
    active              INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS identity (
    identity_id         TEXT PRIMARY KEY,
    identity_code       TEXT NOT NULL UNIQUE,
    identity_name       TEXT NOT NULL,
    display_name_zh     TEXT NOT NULL,
    identity_type       TEXT NOT NULL CHECK (identity_type IN ('sovereign', 'module', 'companion')),
    group_id            TEXT NOT NULL REFERENCES identity_group(group_id),
    actor               TEXT NOT NULL UNIQUE,
    bound_tool_id       TEXT NOT NULL,
    bound_roots         TEXT NOT NULL DEFAULT '[]',
    manifest_required   INTEGER NOT NULL DEFAULT 0,
    manifest_path_template TEXT NOT NULL DEFAULT '',
    manifest_tool_id_field TEXT NOT NULL DEFAULT '',
    manifest_max_bytes  INTEGER NOT NULL DEFAULT 0,
    authentication      TEXT NOT NULL DEFAULT 'governance-policy-issued-capability-token'
);

CREATE TABLE IF NOT EXISTS identity_required_capability (
    identity_id         TEXT NOT NULL REFERENCES identity(identity_id),
    capability          TEXT NOT NULL,
    PRIMARY KEY (identity_id, capability)
);

CREATE TABLE IF NOT EXISTS identity_manifest_requirement (
    identity_id         TEXT NOT NULL REFERENCES identity(identity_id),
    field_path          TEXT NOT NULL,
    expected_value      TEXT NOT NULL,
    PRIMARY KEY (identity_id, field_path)
);

CREATE TABLE IF NOT EXISTS identity_permission (
    identity_id         TEXT NOT NULL REFERENCES identity(identity_id),
    capability          TEXT NOT NULL,
    PRIMARY KEY (identity_id, capability)
);
