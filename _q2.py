import sqlite3, json
conn = sqlite3.connect("_authority_probe2.sqlite3")
for pid in ["A382","A488","A105","A106","A107","A108"]:
    r = conn.execute("select provision_id, subject, rule, prohibition, exception from articles where provision_id=?", (pid,)).fetchone()
    if not r:
        print(pid, "MISSING"); continue
    print("="*15, r[0], r[1], "="*15)
    print("RULE:", r[2][:1500])
    print("PROHIB:", (r[3] or "")[:600])
    print("EXCEPT:", (r[4] or "")[:600])
conn.close()
