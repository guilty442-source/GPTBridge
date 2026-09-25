import os, psycopg
con = psycopg.connect(os.environ["GPTBRIDGE_POSTGRES_DSN"])
tables = [r[0] for r in con.execute(
    "select table_name from information_schema.tables where table_schema='gptbridge_codex' order by table_name")]
print("\n".join(tables))
