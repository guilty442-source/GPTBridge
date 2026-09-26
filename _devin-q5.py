import sys, psycopg
admin = "postgresql://postgres:uUJiXqgO1oiTorXFuoAdYU2UoIvzPA0K@127.0.0.1:5432"
with psycopg.connect(admin + "/postgres", autocommit=True) as c:
    dbs = [r[0] for r in c.execute("SELECT datname FROM pg_database WHERE NOT datistemplate ORDER BY 1")]
for db in dbs:
    try:
        with psycopg.connect(admin + "/" + db, autocommit=True) as cc:
            schemas = [r[0] for r in cc.execute("SELECT table_schema FROM information_schema.tables WHERE table_name='learned_recipes'")]
            rschemas = [r[0] for r in cc.execute("SELECT nspname FROM pg_namespace WHERE nspname='gptbridge_repair'")]
            print(f"{db}: schemas_with_table={schemas} has_gptbridge_repair={bool(rschemas)}")
    except Exception as e:
        print(f"{db}: ERR {e}")
