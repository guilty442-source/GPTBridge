import sqlite3, sys

db = sys.argv[1] if len(sys.argv) > 1 else "governance_rule/codex/data/governance_codex.sqlite3"
con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
tables = [r[0] for r in con.execute("select name from sqlite_master where type='table'")]
print("history-ish tables:", [t for t in tables if any(k in t.lower() for k in ("hist", "version", "meta", "revis", "author"))])
for t in tables:
    if any(k in t.lower() for k in ("hist", "version", "meta", "revis")):
        try:
            cols = [c[1] for c in con.execute(f"pragma table_info({t})")]
            rows = con.execute(f"select * from {t} limit 4").fetchall()
            print("==", t, cols)
            for r in rows:
                print("  ", str(r)[:300])
        except Exception as e:
            print(t, "err", e)
