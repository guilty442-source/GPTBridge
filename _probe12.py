import sys, io, json
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"E:\GPTBridge")
from governance_rule.execution.codex_repository import codex_readonly_connection

with codex_readonly_connection() as conn:
    for pid in ["A341", "A343", "A210", "A211"]:
        cur = conn.execute(
            "SELECT subject, rule, prohibition, exception FROM articles WHERE provision_id=%s", (pid,))
        row = cur.fetchone()
        print(f"### {pid} subj={row[0] if row else 'MISSING'}")
        if row:
            print(f"  RULE: {row[1][:900]}")
            print(f"  PROH: {row[2][:200]}")
            print(f"  EXC : {row[3][:200]}")
    # which of A605-A621 exist in articles
    cur = conn.execute(
        "SELECT provision_id, subject FROM articles WHERE position BETWEEN 605 AND 625 ORDER BY position")
    print("\n=== articles 605-625:")
    for r in cur.fetchall():
        print("  ", r)
    # formal_rule_registry columns
    cur = conn.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_schema='gptbridge_codex' "
        "AND table_name='formal_rule_registry' ORDER BY ordinal_position")
    print("\n=== formal_rule_registry cols:", [r[0] for r in cur.fetchall()])
    cur = conn.execute(
        "SELECT rule_code, precedence_scope, controlling_provision FROM formal_rule_registry "
        "WHERE status='evaluator-parity-verified' ORDER BY rule_code" )
    print("\n=== verified rules -> controlling_provision:")
    for r in cur.fetchall():
        print("  ", r)
