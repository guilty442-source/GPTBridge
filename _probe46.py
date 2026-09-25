import sys, io, json
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"E:\GPTBridge")
from governance_rule.execution.codex_repository import codex_readonly_connection

with codex_readonly_connection() as conn:
    r = conn.execute("SELECT rule, exception FROM articles WHERE provision_id='A604'").fetchone()
    print("A604 RULE FULL:", r[0])
    print("A604 EXC:", r[1])
    print("=" * 80)
    for code in ["RULE_VECTOR_ENGINE_RUST_V1","RULE_JS_NATIVE_V1","RULE_GO_RUST_NODE_V1",
                 "RULE_TEST_TOOLS_V1","RULE_RUST_TESTS_V1","RULE_JAX_PARITY_V1",
                 "RULE_FINAL_ARCHITECTURE_V1"]:
        cur = conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_schema='gptbridge_codex' "
            "AND table_name='formal_rule_registry' ORDER BY ordinal_position")
        cols = [c[0] for c in cur.fetchall()]
        row = conn.execute(
            "SELECT * FROM formal_rule_registry WHERE rule_code=%s AND status<>'withdrawn'", (code,)).fetchone()
        print(code, "->")
        print(json.dumps(dict(zip(cols, row)), ensure_ascii=False, default=str)[:1500])
        print()
