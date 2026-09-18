-- Migration 132: runtime read access to the SQLite classification registry
-- The maintenance controller and saga module-reconcile resolution read
-- gptbridge_index.sqlite_database_class through the runtime role; migration
-- 037 granted SELECT only to the dedicated reader role.

GRANT SELECT ON gptbridge_index.sqlite_database_class TO gptbridge_runtime;
GRANT SELECT ON gptbridge_index.sqlite_database_class TO gptbridge_xingcheng_reader;
