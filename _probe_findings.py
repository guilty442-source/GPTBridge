import sys
sys.path.insert(0, r"E:\GPTBridge")
from governance_rule.execution.codex_repository import codex_readonly_connection

def cols(conn, t):
    cur = conn.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema='gptbridge_codex' AND table_name=%s "
        "ORDER BY ordinal_position", (t,))
    return [r[0] for r in cur.fetchall()]

with codex_readonly_connection() as conn:
    cur = conn.execute("SELECT codex_version FROM codex_authority_state")
    print("=== authority:", cur.fetchone())

    # tables of interest
    cur = conn.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema='gptbridge_codex' ORDER BY table_name")
    tables = [r[0] for r in cur.fetchall()]
    for key in ("version", "revision", "seal", "epoch", "binding", "manifest", "obligation",
                "parity", "classification", "reference", "normative"):
        print(f"-- tables containing '{key}':", [t for t in tables if key in t])

    # articles for language conflict check
    cur = conn.execute("SELECT * FROM articles WHERE provision_id IN ('A35','A341','A343','A347','A348','A350','A351','A353','A355','A356','A359','A360','A362','A604','A199') ORDER BY provision_id")
    acols = cols(conn, "articles")
    print("=== articles cols:", acols)
    for r in cur.fetchall():
        d = dict(zip(acols, r))
        print(d.get("provision_id"), "| status:", d.get("status"), "| rule:", str(d.get("rule"))[:200].replace("\n", " "))
