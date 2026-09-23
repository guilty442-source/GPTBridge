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

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

RESOURCE_INTERVAL_S = 300.0

_POLICY_REL = Path("main-system") / "config" / "native-shadow.json"


def read_policy_mode(component: str, project_root: Path | None) -> str:
    """Return the governed mode for ``component`` ("off"/"shadow"/"primary");
    "off" for absent/invalid policy or a missing component entry."""
    root = Path(project_root) if project_root else Path(
        __file__
    ).resolve().parents[3]
    try:
        policy = json.loads((root / _POLICY_REL).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "off"
    components = policy.get("components")
    entry = (
        (components or {}).get(component) if isinstance(components, dict)
        else None
    )
    return str((entry or {}).get("mode") or "off").strip().lower()


def emit_primary_unavailable(
    component: str, log_path: Path, detail: str
) -> None:
    """Append one ``primary-unavailable`` audit record — a substitution
    failure is observable, never silent."""
    record = {
        "schema": "native-shadow-divergence/v1",
        "component": component,
        "at": datetime.now(timezone.utc).isoformat(),
        "monotonic_s": round(time.monotonic(), 3),
        "kind": "primary-unavailable",
        "detail": detail[:200],
    }
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


def load_native_primary(
    component: str,
    project_root: Path | None,
    factory: Callable[[], Any],
    *,
    log_rel: Path | str,
) -> Optional[Any]:
    """Return the authoritative native object when policy mode is
    ``primary``; ``None`` otherwise or when ``factory`` raises (caller
    falls back to the Python path; the failure is audited via
    ``emit_primary_unavailable`` so it is never silent)."""
    if read_policy_mode(component, project_root) != "primary":
        return None
    root = Path(project_root) if project_root else Path(
        __file__
    ).resolve().parents[3]
    try:
        return factory()
    except Exception as exc:
        emit_primary_unavailable(
            component,
            root / log_rel,
            f"{type(exc).__name__}: {exc}",
        )
        return None


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


__all__ = [
    "maybe_emit_resource",
    "RESOURCE_INTERVAL_S",
    "read_policy_mode",
    "emit_primary_unavailable",
    "load_native_primary",
]
