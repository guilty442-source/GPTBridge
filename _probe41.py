import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, r"E:\GPTBridge")
from governance_rule.execution.codex_repository import codex_readonly_connection

TABLES = ("codex_search_document", "codex_internal_module_membership",
          "codex_internal_module_dependency", "codex_search_alias",
          "codex_search_index_manifest", "seal_manifest", "epoch_seal_manifest",
          "revision_history", "current_normative_surface", "codex_search_fts",
          "codex_search_fts_content", "codex_search_fts_data",
          "codex_search_fts_idx", "codex_search_fts_docsize",
          "codex_search_fts_config", "metadata")
with codex_readonly_connection() as conn:
    for t in TABLES:
        cur = conn.execute(
            "SELECT column_name, ordinal_position FROM information_schema.columns "
            "WHERE table_schema='gptbridge_codex' AND table_name=%s ORDER BY ordinal_position", (t,))
        cols = [r[0] for r in cur.fetchall()]
        print(t, "->", cols)
