import sys
from pathlib import Path

sys.path.insert(0, str(Path("shared-layer/src").resolve()))
import psycopg
from shared_layer.database.migrations import MigrationRunner

ADMIN = "postgresql://postgres:uUJiXqgO1oiTorXFuoAdYU2UoIvzPA0K@127.0.0.1:5432/gptbridge"
conn = psycopg.connect(ADMIN, autocommit=False, row_factory=psycopg.rows.dict_row)
runner = MigrationRunner(Path("shared-layer/migrations"))
result = runner.run(conn)
conn.commit()
print("applied:", result.applied)
