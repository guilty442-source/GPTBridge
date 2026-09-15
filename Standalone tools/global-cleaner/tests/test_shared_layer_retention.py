"""global-cleaner consolidated test suite (A57/E43)

One managed test file per module, maintained by the
maintenance sovereign for self-health (self-test collection).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
for _p in (
    str(_ROOT),
    str(_ROOT / "shared-layer" / "src"),
    str(_ROOT / "main-system" / "src-core"),
    str(_ROOT / "main-system"),
    str(_ROOT / "main-system" / "src" / "backend" / "services"),
    str(_ROOT / "Standalone tools" / "local-model" / "src" / "backend" / "services"),
    str(_ROOT / "Standalone tools" / "ai-assistant" / "src"),
    str(_ROOT / "Standalone tools" / "ai-assistant" / "src" / "backend" / "services"),
    str(_ROOT / "Standalone tools" / "ai-collaboration" / "src" / "backend" / "services"),
    str(_ROOT / "Standalone tools" / "file-sorter" / "src" / "backend" / "services"),
    str(_ROOT / "Standalone tools" / "investment-mobile" / "src" / "backend" / "services"),
    str(_ROOT / "Standalone tools" / "vaultly" / "src" / "backend" / "services"),
    # Own tool path last: insert(0) makes it win over other tools' `backend`
    # packages (several tools ship a top-level `backend` package).
    str(_ROOT / "Standalone tools" / "global-cleaner" / "src"),
):
    if _p not in sys.path:
        sys.path.insert(0, _p)

del _p

########################################################################
# source: global-cleaner/tests/test_shared_layer_retention.py
########################################################################
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SHARED_SRC = ROOT / "shared-layer" / "src"
if str(SHARED_SRC) not in sys.path:
    sys.path.insert(0, str(SHARED_SRC))


from shared_layer.store import SharedLayerStore  # noqa: E402


def test_store_uses_postgres_transport_terminating_consumed_rows() -> None:
    source = Path(
        __file__
    ).resolve().parents[3] / "shared-layer" / "src" / "shared_layer" / "store.py"
    text = source.read_text("utf-8")

    assert hasattr(SharedLayerStore, "consume_response")
    assert "gptbridge_transport.tool_request" in text
    assert "pg_notify" in text
    assert "FOR UPDATE SKIP LOCKED" in text

    assert "def _maintain_requests" not in text
    assert "_last_request_maintenance" not in text
