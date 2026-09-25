import os, psycopg
dsn = os.environ.get("GPTBRIDGE_POSTGRES_DSN")
con = psycopg.connect(dsn)
for t in ("codex_version_epochs","epoch_seal_manifest","revision_history","articles"):
    try:
        cols = [r[0] for r in con.execute(
            "select column_name from information_schema.columns where table_name=%s order by ordinal_position", (t,))]
        print(t, "=>", cols)
    except Exception as e:
        con.rollback(); print(t, "ERR", e)
