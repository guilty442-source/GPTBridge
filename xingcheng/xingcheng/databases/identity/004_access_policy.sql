REVOKE ALL ON SCHEMA role_data,role_history,role_audit FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA role_data,role_history,role_audit FROM PUBLIC;
DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='gptbridge_xingcheng_internal') THEN
CREATE ROLE gptbridge_xingcheng_internal NOLOGIN; END IF; END $$;
GRANT USAGE ON SCHEMA role_data,role_history,role_audit TO gptbridge_xingcheng_internal;
GRANT SELECT,INSERT,UPDATE,DELETE ON ALL TABLES IN SCHEMA role_data,role_history,role_audit TO gptbridge_xingcheng_internal;
ALTER TABLE role_data.personality ENABLE ROW LEVEL SECURITY;
ALTER TABLE role_data.personality FORCE ROW LEVEL SECURITY;
ALTER TABLE role_history.personality_version ENABLE ROW LEVEL SECURITY;
ALTER TABLE role_history.personality_version FORCE ROW LEVEL SECURITY;
ALTER TABLE role_audit.event ENABLE ROW LEVEL SECURITY;
ALTER TABLE role_audit.event FORCE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS xingcheng_internal_personality ON role_data.personality;
CREATE POLICY xingcheng_internal_personality ON role_data.personality FOR ALL USING (pg_has_role(current_user,'gptbridge_xingcheng_internal','member')) WITH CHECK (pg_has_role(current_user,'gptbridge_xingcheng_internal','member'));
DROP POLICY IF EXISTS xingcheng_internal_personality_version ON role_history.personality_version;
CREATE POLICY xingcheng_internal_personality_version ON role_history.personality_version FOR ALL USING (pg_has_role(current_user,'gptbridge_xingcheng_internal','member')) WITH CHECK (pg_has_role(current_user,'gptbridge_xingcheng_internal','member'));
DROP POLICY IF EXISTS xingcheng_internal_role_audit ON role_audit.event;
CREATE POLICY xingcheng_internal_role_audit ON role_audit.event FOR ALL USING (pg_has_role(current_user,'gptbridge_xingcheng_internal','member')) WITH CHECK (pg_has_role(current_user,'gptbridge_xingcheng_internal','member'));
