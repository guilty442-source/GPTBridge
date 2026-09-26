import sys
sys.path.insert(0, "shared-layer/src")
from shared_layer.local.pg_adapter import connect as pg_connect
conn = pg_connect("gptbridge_xingcheng")
for row in conn.execute(
    "SELECT column_name, data_type, is_identity, column_default "
    "FROM information_schema.columns "
    "WHERE table_schema = current_schema() "
    "AND table_name = 'transformer_training_audit_event' ORDER BY ordinal_position"
).fetchall():
    print(dict(row))
