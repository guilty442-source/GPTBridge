import sqlite3
conn = sqlite3.connect('E:/GPTBridge/governance_rule/codex/data/governance_codex.sqlite3')
conn.row_factory = sqlite3.Row
cur = conn.cursor()

print("=== ARTICLES ===")
cur.execute("SELECT provision_id, subject, rule, prohibition, exception FROM articles ORDER BY position")
for row in cur:
    print(f"  {row['provision_id']}: {row['subject'][:100]}")
    print(f"    RULE: {row['rule'][:200]}")
    if row['prohibition']:
        print(f"    PROHIBIT: {row['prohibition'][:200]}")
    if row['exception']:
        print(f"    EXCEPT: {row['exception'][:200]}")

print("\n=== EDICTS ===")
cur.execute("SELECT provision_id, area, edict, immutability FROM edicts ORDER BY position")
for row in cur:
    print(f"  {row['provision_id']} ({row['area']}): {row['edict'][:200]}")
    print(f"    IMMUT: {row['immutability']}")

print("\n=== FORMAL RULES ===")
cur.execute("SELECT rule_code, controlling_provision_id, predicate, pass_decision, fail_decision, severity FROM formal_rule_registry ORDER BY rule_code")
for row in cur:
    print(f"  {row['rule_code']} <- {row['controlling_provision_id']}")
    print(f"    PRED: {row['predicate'][:200]}")
    print(f"    PASS: {row['pass_decision']} | FAIL: {row['fail_decision']} | SEV: {row['severity']}")

print("\n=== MACHINE SCHEMAS ===")
cur.execute("SELECT schema_code, schema_kind, semantic_owner, required_fields, status FROM machine_schema_registry ORDER BY schema_code")
for row in cur:
    print(f"  {row['schema_code']} ({row['schema_kind']}): owner={row['semantic_owner']}, fields={row['required_fields'][:80]}")

print("\n=== SOVEREIGN DUTIES ===")
cur.execute("SELECT sovereign_id, position, value FROM sovereign_duties ORDER BY sovereign_id, position")
for row in cur:
    print(f"  {row['sovereign_id']} [{row['position']}]: {row['value'][:150]}")

print("\n=== SOVEREIGN POWERS ===")
cur.execute("SELECT sovereign_id, position, value FROM sovereign_powers ORDER BY sovereign_id, position")
for row in cur:
    print(f"  {row['sovereign_id']} [{row['position']}]: {row['value'][:150]}")

print("\n=== SOVEREIGN PROHIBITIONS ===")
cur.execute("SELECT sovereign_id, position, value FROM sovereign_prohibitions ORDER BY sovereign_id, position")
for row in cur:
    print(f"  {row['sovereign_id']} [{row['position']}]: {row['value'][:150]}")