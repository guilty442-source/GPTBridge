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

DROP POLICY IF EXISTS resource_write ON gptbridge_index.resource;
CREATE POLICY resource_write ON gptbridge_index.resource FOR ALL
    USING (gptbridge_security.can_write_resource(module_id, classification))
    WITH CHECK (gptbridge_security.can_write_resource(module_id, classification));

DROP POLICY IF EXISTS location_owner_write ON registry.locations;
CREATE POLICY location_owner_write ON registry.locations FOR ALL
    USING (
        gptbridge_security.can_write(module_id)
        AND EXISTS (
            SELECT 1 FROM gptbridge_index.resource AS resource
            WHERE resource.resource_id = locations.resource_id
              AND gptbridge_security.can_write_resource(
                  resource.module_id, resource.classification
              )
        )
    )
    WITH CHECK (
        gptbridge_security.can_write(module_id)
        AND EXISTS (
            SELECT 1 FROM gptbridge_index.resource AS resource
            WHERE resource.resource_id = locations.resource_id
              AND gptbridge_security.can_write_resource(
                  resource.module_id, resource.classification
              )
        )
    );
