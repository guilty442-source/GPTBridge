import sys
from pathlib import Path

import psycopg

ADMIN = "postgresql://postgres:uUJiXqgO1oiTorXFuoAdYU2UoIvzPA0K@127.0.0.1:5432/gptbridge"
conn = psycopg.connect(ADMIN, autocommit=False)
for name in ("146_governance_nonce.sql", "147_state_outbox.sql"):
    body = Path(f"shared-layer/migrations/{name}").read_text(encoding="utf-8")
    conn.execute("SET LOCAL gptbridge.is_migration_executor = 'true'")
    conn.execute(body)
    conn.commit()
    print("applied", name)
conn.close()
