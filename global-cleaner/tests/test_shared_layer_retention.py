from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SHARED_SRC = ROOT / "shared-layer" / "src"
if str(SHARED_SRC) not in sys.path:
    sys.path.insert(0, str(SHARED_SRC))

from shared_layer.store import SharedLayerStore  # noqa: E402


def test_store_uses_postgres_transport_terminating_consumed_rows() -> None:
    source = Path(
        __file__
    ).resolve().parents[2] / "shared-layer" / "src" / "shared_layer" / "store.py"
    text = source.read_text("utf-8")

    assert hasattr(SharedLayerStore, "consume_response")
    assert "gptbridge_transport.tool_request" in text
    assert "pg_notify" in text
    assert "FOR UPDATE SKIP LOCKED" in text

    assert "def _maintain_requests" not in text
    assert "_last_request_maintenance" not in text
