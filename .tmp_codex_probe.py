from governance_rule.execution.codex_repository import codex_readonly_connection

with codex_readonly_connection() as connection:
    print("codex_version", connection.execute("SELECT value FROM metadata WHERE key='codex_version'").fetchone())
    print("history", connection.execute("SELECT sequence, change_id, version, entry_hash FROM revision_history ORDER BY sequence DESC LIMIT 1").fetchone())
    rows = connection.execute(
        "SELECT rule_code, controlling_provision_id, status, parity_status, parity_evidence_id "
        "FROM formal_rule_registry WHERE status='proposed' ORDER BY rule_code"
    ).fetchall()
    print("proposed_count", len(rows))
    for row in rows:
        print(row)
