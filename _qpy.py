import os, psycopg
con = psycopg.connect(os.environ["GPTBRIDGE_POSTGRES_DSN"])
for pid in ("A35","A211","A264","A343","A610"):
    r = con.execute("select rule from gptbridge_codex.articles where provision_id=%s",(pid,)).fetchone()
    print("=====", pid, "====="); print(r[0]); print()
