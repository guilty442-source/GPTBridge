import sys
sys.path.insert(0, "shared-layer/src")
from shared_layer.security.dsn_policy import resolve_dsn, DsnPurpose
import psycopg

conn = psycopg.connect(
    resolve_dsn(DsnPurpose.RUNTIME).dsn,
    options="-c search_path=gptbridge_codex,pg_catalog -c default_transaction_read_only=on",
)
r = conn.execute("select rule from articles where provision_id='A615'").fetchone()
print("A615:", (r[0][:400] if r else None))
r = conn.execute("select rule from articles where provision_id='A8'").fetchone()
print()
print("A8:", (r[0][:400] if r else None))
r = conn.execute("select subject from articles where provision_id='A208'").fetchone()
print()
print("A208 subject:", r)
r = conn.execute("select subject from articles where provision_id='A209'").fetchone()
print("A209 subject:", r)
r = conn.execute("select subject from articles where provision_id='A210'").fetchone()
print("A210 subject:", r)
for r in conn.execute(
    "select entry_code, status from a233_normalized_directory_entry "
    "where entry_code like 'QDRANT%' or entry_code like '%SQLITE%' limit 12"
):
    print("a233:", r)
cols = [c[0] for c in conn.execute(
    "select column_name from information_schema.columns "
    "where table_name='architecture_activation_states'")]
print("act cols:", cols)
print()
rule615 = conn.execute("select rule from articles where provision_id='A615'").fetchone()[0]
print("A615 len:", len(rule615))
for token in ["0.11.2", "1.13.0", "2.5.3", "19.2.8", "18.6", "2.55.0", "1.98.1", "1.27.1", "3.14.7"]:
    print("  ", token, token in rule615)
for r in conn.execute(
    "select architecture_code, current_state, required_state "
    "from architecture_activation_states "
    "where architecture_code like '%QDRANT%' or architecture_code like '%VECTORD%' "
    "or target_root like '%qdrant%'"
):
    print("act:", r)
