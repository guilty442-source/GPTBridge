from __future__ import annotations

import sqlite3
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SHARED_SRC = ROOT / "shared-layer" / "src"
if str(SHARED_SRC) not in sys.path:
    sys.path.insert(0, str(SHARED_SRC))

from shared_layer.store import SharedLayerStore  # noqa: E402


def test_request_maintenance_requeues_stale_claims_and_caps_terminal_rows() -> None:
    store = object.__new__(SharedLayerStore)
    store._last_request_maintenance = 0
    connection = sqlite3.connect(":memory:")
    connection.execute(
        "CREATE TABLE tool_requests ("
        "request_id TEXT PRIMARY KEY, requester_actor TEXT NOT NULL, "
        "target_tool_id TEXT NOT NULL, payload_json TEXT NOT NULL, "
        "status TEXT NOT NULL, response_json TEXT, "
        "created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL)"
    )
    now = 200_000
    rows = [
        (f"done-{index:04d}", "actor", "local-ai", "{}", "completed", "{}", now, now)
        for index in range(1_050)
    ]
    rows.append(
        ("stale-claim", "actor", "local-ai", "{}", "claimed", None, now - 400, now - 400)
    )
    rows.append(
        ("stale-queued", "actor", "local-ai", "{}", "queued", None, now - 4_000, now - 4_000)
    )
    connection.executemany(
        "INSERT INTO tool_requests VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )

    store._maintain_requests(connection, now)

    terminal_count = connection.execute(
        "SELECT COUNT(*) FROM tool_requests "
        "WHERE status IN ('completed', 'cancelled')"
    ).fetchone()[0]
    stale_status = connection.execute(
        "SELECT status FROM tool_requests WHERE request_id = 'stale-claim'"
    ).fetchone()[0]
    stale_queued_status = connection.execute(
        "SELECT status FROM tool_requests WHERE request_id = 'stale-queued'"
    ).fetchone()[0]
    assert terminal_count == 100
    assert stale_status == "queued"
    assert stale_queued_status == "cancelled"
