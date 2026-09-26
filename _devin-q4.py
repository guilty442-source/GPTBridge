import sys
sys.path.insert(0, ".")
sys.path.insert(0, "shared-layer/src")
import psycopg
dsn = "postgresql://postgres:uUJiXqgO1oiTorXFuoAdYU2UoIvzPA0K@127.0.0.1:5432/postgres"
with psycopg.connect(dsn, autocommit=True) as c:
    print("databases:")
    for r in c.execute("SELECT datname FROM pg_database WHERE NOT datistemplate ORDER BY 1"):
        print(" ", r[0])
    for db in ("gptbridge",):
        with psycopg.connect(f"postgresql://postgres:uUJiXqgO1oiTorXFuoAdYU2UoIvzPA0K@127.0.0.1:5432/{db}", autocommit=True) as cc:
            rows = cc.execute("SELECT table_schema FROM information_schema.tables WHERE table_name='learned_recipes'").fetchall()
            print(db, "learned_recipes in schemas:", [r[0] for r in rows])
            rows2 = cc.execute("SELECT nspname FROM pg_namespace WHERE nspname='gptbridge_repair'").fetchall()
            print(db, "gptbridge_repair schema:", bool(rows2))
