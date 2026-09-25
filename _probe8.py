import sys, re
sys.path.insert(0, r"E:\GPTBridge")
from governance_rule.execution.codex_repository import codex_readonly_connection

def cols(conn, t):
    cur = conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema='gptbridge_codex' AND table_name=%s ORDER BY ordinal_position", (t,))
    return [r[0] for r in cur.fetchall()]

with codex_readonly_connection() as conn:
    # obligations — find the one mentioning machine schema parity / 74
    cur = conn.execute("SELECT obligation_code, current_state, target_state, acceptance_evidence FROM implementation_obligations")
    for code, state, target, ev in cur.fetchall():
        blob = f"{code} {target} {ev}"
        if "machine" in blob.lower() or "74" in blob or "parity" in blob.lower():
            print("OBL:", code, "|", state, "|", str(target)[:140], "|", str(ev)[:100])

    # superseded references: active provisions whose rule references superseded ids
    cur = conn.execute("SELECT provision_id, lifecycle_state FROM provision_lifecycle_status")
    state = {p: s for p, s in cur.fetchall()}
    superseded = {p for p, s in state.items() if s == "superseded"}
    cur = conn.execute("SELECT provision_id, rule, prohibition, exception FROM articles")
    refpat = re.compile(r"\b([APESH]\d{2,4})\b")
    rows = cur.fetchall()
    offenders = {}
    for pid, rule, pro, exc in rows:
        if state.get(pid) != "active":
            continue
        text = f"{rule or ''} {pro or ''} {exc or ''}"
        refs = {m for m in refpat.findall(text) if m in superseded}
        if refs:
            offenders[pid] = sorted(refs)
    print("active provisions referencing superseded:", len(offenders))
    for k, v in list(offenders.items())[:80]:
        print("  ", k, "->", v)

    # reference resolution tables
    for t in ("provision_reference_resolution", "provision_reference_resolution_v2"):
        c = cols(conn, t)
        cur = conn.execute(f"SELECT * FROM {t}")
        rows2 = cur.fetchall()
        print(f"=== {t} {c} rows={len(rows2)}")
        for r in rows2[:4]:
            print("   ", {k: str(v)[:70] for k, v in zip(c, r)})
