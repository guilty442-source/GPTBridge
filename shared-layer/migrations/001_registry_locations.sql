CREATE SCHEMA IF NOT EXISTS registry;

CREATE TABLE IF NOT EXISTS registry.locations (
    locator_id uuid PRIMARY KEY,
    resource_id text NOT NULL UNIQUE
        REFERENCES gptbridge_index.resource(resource_id) ON DELETE CASCADE,
    module_id text NOT NULL,
    executor_type text NOT NULL CHECK (
        executor_type IN ('file', 'system', 'model', 'local-logic')
    ),
    location_key text NOT NULL,
    physical_location text NOT NULL,
    content_hash text,
    version bigint NOT NULL DEFAULT 1 CHECK (version > 0),
    status text NOT NULL DEFAULT 'active' CHECK (
        status IN ('active', 'missing', 'moving', 'archived', 'deleted')
    ),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (module_id, location_key)
);

CREATE INDEX IF NOT EXISTS locations_module_status_idx
    ON registry.locations(module_id, status);

ALTER TABLE registry.locations ENABLE ROW LEVEL SECURITY;
ALTER TABLE registry.locations FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS location_owner_read ON registry.locations;
CREATE POLICY location_owner_read ON registry.locations FOR SELECT
    USING (gptbridge_security.can_read(module_id));

DROP POLICY IF EXISTS location_owner_write ON registry.locations;
CREATE POLICY location_owner_write ON registry.locations FOR ALL
    USING (gptbridge_security.can_write(module_id))
    WITH CHECK (gptbridge_security.can_write(module_id));

REVOKE ALL ON SCHEMA registry FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA registry FROM PUBLIC;
GRANT USAGE ON SCHEMA registry TO gptbridge_index_executor;
GRANT SELECT, INSERT, UPDATE, DELETE ON registry.locations TO gptbridge_index_executor;

-- 星澄只能透過安全檢視取得 opaque locator；physical_location 永不公開。
CREATE OR REPLACE VIEW registry.resource_locations
WITH (security_barrier=true) AS
SELECT locator_id, resource_id, module_id, executor_type, location_key,
       content_hash, version, status, created_at, updated_at
FROM registry.locations
WHERE gptbridge_security.can_read(module_id);

GRANT USAGE ON SCHEMA registry TO gptbridge_xingcheng_reader;
GRANT SELECT ON registry.resource_locations TO gptbridge_xingcheng_reader;
