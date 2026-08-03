ALTER TABLE gptbridge_index.resource
    ADD COLUMN IF NOT EXISTS logical_key text;

UPDATE gptbridge_index.resource
SET logical_key = resource_label
WHERE logical_key IS NULL;

ALTER TABLE gptbridge_index.resource
    ALTER COLUMN logical_key SET NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS resource_module_logical_key_uidx
    ON gptbridge_index.resource(module_id, logical_key);

CREATE OR REPLACE FUNCTION gptbridge_index.touch_resource()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at = now();
    IF ROW(NEW.*) IS DISTINCT FROM ROW(OLD.*) THEN
        NEW.version = OLD.version + 1;
    END IF;
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS resource_touch ON gptbridge_index.resource;
CREATE TRIGGER resource_touch BEFORE UPDATE ON gptbridge_index.resource
FOR EACH ROW EXECUTE FUNCTION gptbridge_index.touch_resource();

CREATE OR REPLACE FUNCTION registry.touch_location()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at = now();
    IF ROW(NEW.*) IS DISTINCT FROM ROW(OLD.*) THEN
        NEW.version = OLD.version + 1;
    END IF;
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS location_touch ON registry.locations;
CREATE TRIGGER location_touch BEFORE UPDATE ON registry.locations
FOR EACH ROW EXECUTE FUNCTION registry.touch_location();
