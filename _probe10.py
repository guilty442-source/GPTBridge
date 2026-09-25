import sys
sys.path.insert(0, r"E:\GPTBridge")
from governance_rule.execution.codex_repository import codex_readonly_connection

TABLES = ["articles", "provision_identities", "provision_lifecycle_status",
          "provision_law_classification", "provision_normative_category",
          "codex_article_classification", "provision_normativity_classification",
          "codex_internal_module_membership", "current_normative_surface",
          "provision_lineage"]
with codex_readonly_connection() as conn:
    def cols(t):
        cur = conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='gptbridge_codex' AND table_name=%s ORDER BY ordinal_position", (t,))
        return [r[0] for r in cur.fetchall()]
    for t in TABLES:
        c = cols(t)
        cur = conn.execute(f"SELECT * FROM {t} WHERE provision_id='A610'" if "provision_id" in c
                           else (f"SELECT * FROM {t} WHERE object_identity='A610'" if "object_identity" in c
                                 else (f"SELECT * FROM {t} WHERE member_provision_id='A610'" if "member_provision_id" in c else "SELECT 1")))
        rows = cur.fetchall() if cur else []
        print(f"=== {t} {c}")
        for r in rows:
            print("   ", {k: str(v)[:70] for k, v in zip(c, r)})
    # also check what tables have A610 / A609 rows to learn required companions
    cur = conn.execute("SELECT * FROM articles WHERE provision_id='A610'")
    print("A610 article:", cur.fetchone())
