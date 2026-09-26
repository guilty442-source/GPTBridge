import sys, tempfile
sys.path.insert(0, "shared-layer/src")
sys.path.insert(0, "Standalone tools/local-model/src/backend/services")
from pathlib import Path
from shared_layer.local.pg_adapter import connect as pg_connect
from xingcheng.infrastructure.transformer_training_repository import TransformerTrainingRepository
repo = TransformerTrainingRepository(Path(tempfile.mkdtemp()))
conn = pg_connect("gptbridge_xingcheng")
row = conn.execute(
    "SELECT is_identity, column_default FROM information_schema.columns "
    "WHERE table_schema=current_schema() AND table_name='transformer_training_audit_event' "
    "AND column_name='sequence'"
).fetchone()
print("sequence:", dict(row))
