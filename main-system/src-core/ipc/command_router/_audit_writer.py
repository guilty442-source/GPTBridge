"""Shared JSONL audit-ledger append (AA12).

Single implementation of the durable audit append pattern used by the
command-router handlers — one line per record, sort_keys for stable
diffs, best-effort (audit persistence never raises into the request path).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def append_audit_record(ledger: Path, record: dict[str, Any]) -> None:
    """Append one JSON record to ``ledger`` (JSONL). Best-effort."""
    try:
        ledger.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False, sort_keys=True, default=str)
        with ledger.open("a", encoding="utf-8") as handle:
            handle.write(line + os.linesep)
            handle.flush()
    except OSError:
        pass  # audit persistence is best-effort


__all__ = ["append_audit_record"]
