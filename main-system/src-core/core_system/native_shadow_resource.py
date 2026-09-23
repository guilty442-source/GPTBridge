"""Shared resource-budget observation for §10.65 shadow harnesses (P4).

Parity dimension that was systematically absent: bounded-resource
evidence.  Each shadow harness calls ``maybe_emit_resource`` at the top
of its hottest observe path; at most one ``resource-observation`` record
is emitted per component per ``RESOURCE_INTERVAL_S`` so quiet windows
still produce boundedness evidence without flooding the divergence log.

Measurements are process-level (private_bytes / working_set_bytes from
the native helpers) plus the harness's declared native-side counter when
the harness exposes one — the record documents that the dual-track
window stayed inside budget; it never affects the authoritative path.
"""

from __future__ import annotations

import time
from typing import Any

RESOURCE_INTERVAL_S = 300.0


def _resource_snapshot() -> dict[str, Any]:
    snap: dict[str, Any] = {}
    try:
        from core_system.native import _sovereign_native as native

        snap["private_bytes"] = int(native.private_bytes())
        snap["working_set_bytes"] = int(native.working_set_bytes())
    except Exception:
        pass
    return snap


def maybe_emit_resource(shadow: Any) -> None:
    """Emit a throttled resource-observation record for ``shadow``.

    Writes to a sibling ``<divergence-log>.resources.jsonl`` — the
    divergence log keeps its "record exists only on divergence/disable"
    contract untouched.  Requires the harness to expose ``_log_path`` and
    ``_COMPONENT``/``component`` is derived from it; tolerates a missing
    ``_last_resource_emit`` attribute (first call always emits).  Any
    failure is swallowed — observation must never disturb the
    authoritative path or the harness's own fail-closed disable logic.
    """
    try:
        now = time.monotonic()
        last = getattr(shadow, "_last_resource_emit", None)
        if last is not None and (now - float(last)) < RESOURCE_INTERVAL_S:
            return
        shadow._last_resource_emit = now
        log_path = getattr(shadow, "_log_path", None)
        if log_path is None:
            return
        from datetime import datetime, timezone
        import json as _json

        target = log_path.with_suffix(".resources.jsonl")
        record = {
            "schema": "native-shadow-resource/v1",
            "component": log_path.stem,
            "at": datetime.now(timezone.utc).isoformat(),
            "monotonic_s": round(now, 3),
            "kind": "resource-observation",
            "interval_s": RESOURCE_INTERVAL_S,
            "resources": _resource_snapshot(),
        }
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(_json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


__all__ = ["maybe_emit_resource", "RESOURCE_INTERVAL_S"]
