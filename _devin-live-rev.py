import sys
sys.path.insert(0, "shared-layer/src")
from shared_layer.security.dsn_policy import resolve_dsn, DsnPurpose
import psycopg

conn = psycopg.connect(
    resolve_dsn(DsnPurpose.RUNTIME).dsn,
    options="-c search_path=gptbridge_codex,pg_catalog -c default_transaction_read_only=on",
)
for r in conn.execute("select * from codex_authority_state").fetchall():
    print("state:", str(r)[:500])
r = conn.execute(
    "select sequence, version, entry_hash, version_epoch from revision_history order by sequence desc limit 1"
).fetchone()
print("head:", r)
for r in conn.execute("select epoch, baseline_version, version_identity, status from codex_version_epochs order by epoch desc limit 2"):
    print("epoch:", r)
