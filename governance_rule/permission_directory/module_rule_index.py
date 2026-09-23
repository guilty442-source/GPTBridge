"""Module rule index — permission-core-owned, module-derived store and scoped query.

A607: The permission core owns the index lifecycle; each owning module governs
its own content slice.  The index is module-derived, non-authoritative, and
must have zero mixing with the runtime rule index (which indexes A<digits>
provisions).  Storage is the derived JSON file under
``main-system/runtime/state/module-rule-index.json``; generation is atomic
(via the builder script) and validated by ``content_sha256``.

This module provides the runtime store and scoped query service that the
blueprint requires for ``MODULE_RULE_INDEX_V1``.  It intentionally does not
import or read the runtime index beyond the zero-mixing check.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INDEX = PROJECT_ROOT / "main-system" / "runtime" / "state" / "module-rule-index.json"
RUNTIME_INDEX = PROJECT_ROOT / "main-system" / "runtime" / "state" / "runtime-rule-index.json"

_SCHEMA = "gptbridge-module-rule-index/v1"


def _load() -> dict[str, Any]:
    try:
        data = json.loads(DEFAULT_INDEX.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"module rule index unavailable: {exc}") from exc
    return data if isinstance(data, dict) else {}


def _validate(data: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if data.get("schema") != _SCHEMA:
        errors.append(f"schema mismatch: expected {_SCHEMA}")
    # zero mixing: no module_id may look like a runtime provision A<digits>
    for mid in (data.get("modules") or {}).keys():
        if re.fullmatch(r"A\d{1,4}", str(mid)):
            errors.append(f"module_id collides with runtime provision: {mid}")
    # also ensure no overlap with runtime index keys when present
    if RUNTIME_INDEX.is_file():
        try:
            runtime = json.loads(RUNTIME_INDEX.read_text(encoding="utf-8"))
            runtime_ids = set((runtime.get("provisions") or {}).keys())
            for r in runtime.get("rules") or []:
                if isinstance(r, dict) and r.get("rule_code"):
                    runtime_ids.add(str(r.get("rule_code")))
            overlap = set((data.get("modules") or {}).keys()) & runtime_ids
            if overlap:
                errors.append(f"zero-mixing violated: {sorted(overlap)[:3]}")
        except (OSError, ValueError):
            pass
    return errors


def load_index(*, validate: bool = True) -> dict[str, Any]:
    """Load and optionally validate the module rule index."""
    data = _load()
    if validate:
        errs = _validate(data)
        if errs:
            raise RuntimeError("module index validation failed: " + "; ".join(errs))
    return data


def query(
    *,
    owner: str | None = None,
    role: str | None = None,
    lifecycle: str | None = None,
    module_id: str | None = None,
) -> list[dict[str, Any]]:
    """Scoped query — owning module governs its slice, permission core owns the index.

    Args:
        owner: filter by ``owner_sovereign`` (exact match)
        role: filter by ``architectural_role``
        lifecycle: filter by ``lifecycle``
        module_id: exact module lookup (returns 0 or 1 entry)

    Returns:
        List of module entries matching all supplied filters.  Empty when the
        index is unavailable; callers must treat that as “no evidence”, not as
        authority.
    """
    try:
        data = load_index(validate=True)
    except RuntimeError:
        return []
    modules: dict[str, Any] = data.get("modules") or {}
    if module_id is not None:
        entry = modules.get(module_id)
        return [entry] if isinstance(entry, dict) else []
    result: list[dict[str, Any]] = []
    for mid, entry in modules.items():
        if not isinstance(entry, dict):
            continue
        if owner is not None and entry.get("owner") != owner:
            continue
        if role is not None and entry.get("architectural_role") != role:
            continue
        if lifecycle is not None and entry.get("lifecycle") != lifecycle:
            continue
        result.append(entry)
    return sorted(result, key=lambda x: x.get("module_id", ""))


def counts() -> dict[str, int]:
    try:
        data = load_index(validate=False)
        return dict(data.get("counts") or {})
    except RuntimeError:
        return {}


__all__ = ["counts", "load_index", "query"]
