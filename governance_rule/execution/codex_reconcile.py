"""Self-declaration reconciliation + one-shot bounded lookups (A435).

法典依據:
- A435 SELF_DECLARATION_LOCAL: import-time module self-declaration loads
  only module-authored static typed identifiers and performs no codex
  read; after imports settle the process submits ONE declaration-set hash
  for once-per-process-generation reconciliation against the official
  entry before affected capability activation.
- A435 BOUNDED_MACHINE_LOOKUP: ``bounded_lookup`` wraps one governed
  context around a caller-supplied read so a single-shot consumer does
  not hand-manage session lifecycle.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Callable, Mapping

from governance_rule.execution import codex_entry_state as _state
from governance_rule.execution.codex_session import (
    CodexReadSession,
    open_bounded_context,
)


_reconciled_declarations: dict[tuple[str, str], bool] = {}


def bounded_lookup(
    actor: str,
    *,
    purpose: str,
    scope: tuple[str, ...],
    reader: Callable[[CodexReadSession], Any],
) -> Any:
    """One-shot bounded lookup: open context, run ``reader``, close."""
    with open_bounded_context(actor, purpose=purpose, scope=scope) as context:
        return reader(context)


def reconcile_self_declarations(
    module_code: str,
    declarations: Mapping[str, Any],
    *,
    actor: str,
) -> bool:
    """Reconcile one module's self-declaration set with the official entry.

    Runs once per (actor, module_code) per process generation.  The
    declaration set is hashed canonically; the module identity is checked
    against the registered ``module_assignment_registry`` through a bounded
    context.  The audit record carries the hash + identity + result only —
    never declaration content.
    """
    module = str(module_code or "").strip()
    actor = str(actor or "").strip()
    key = (actor, module)
    if key in _reconciled_declarations:
        return _reconciled_declarations[key]
    declaration_hash = hashlib.sha256(
        json.dumps(
            declarations, ensure_ascii=False, sort_keys=True, default=str
        ).encode("utf-8")
    ).hexdigest()
    result = False
    try:
        with open_bounded_context(
            actor or "governance-audit",
            purpose="self-declaration",
            scope=("registry:module_assignment_registry",),
        ) as context:
            result = any(
                row.get("module_architecture_code") == module
                and row.get("status") == "active"
                for row in context.registry("module_assignment_registry")
            )
    except PermissionError:
        result = False
    _state.record_session_audit(
        event="self-declaration-reconcile",
        actor=actor,
        purpose="self-declaration",
        access_class="self-declaration-local",
        scope=frozenset({f"registry:module_assignment_registry"}),
        codex_version=None,
        correlation=declaration_hash[:16],
        result="RECONCILED" if result else "REJECTED",
    )
    _reconciled_declarations[key] = result
    return result


__all__ = ["bounded_lookup", "reconcile_self_declarations"]
