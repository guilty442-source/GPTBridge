import sys
sys.path.insert(0, "shared-layer/src")
from shared_layer.database.workload_lanes import WorkloadClass, get_lane_pool
with get_lane_pool().connection(WorkloadClass.BACKGROUND) as conn:
    # Try to find codex-related tables
    rows = conn.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='gptbridge_codex' LIMIT 20").fetchall()
    print("tables", rows)
    try:
        r = conn.execute("SELECT * FROM gptbridge_codex.revision_history ORDER BY sequence DESC LIMIT 1").fetchone()
        print("rev keys", list(r.keys()) if r else None)
        print("rev", dict(r) if r else None)
    except Exception as e:
        print("rev fail", e)
        conn.execute("ROLLBACK")
