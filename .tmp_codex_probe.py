from governance_rule.execution.codex_repository import codex_readonly_connection

with codex_readonly_connection() as connection:
    for table in ("metadata", "revision_history", "seal_manifest", "epoch_seal_manifest", "formal_rule_registry"):
        print(table, connection.execute(f"PRAGMA table_info({table})").fetchall())
    print("metadata", connection.execute("SELECT key, value FROM metadata ORDER BY key").fetchall())
    print("revision_tail", connection.execute("SELECT * FROM revision_history ORDER BY rowid DESC LIMIT 3").fetchall())
    print("seal_tail", connection.execute("SELECT * FROM seal_manifest ORDER BY rowid DESC LIMIT 3").fetchall())
