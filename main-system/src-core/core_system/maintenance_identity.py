"""Maintenance identity helper — codex declaration for the health-maintenance domain.

The ``health-maintenance-test-sub-sovereign`` identity is retired
(A592/A604: sub-sovereign layer eliminated); its responsibilities are
absorbed by decision-core.  The maintenance mixins still read the codex
self-declaration for duties/area, so this module exposes the declaration
lookup without importing the removed sub-sovereign implementation.
"""

from __future__ import annotations

from typing import Any


def maintenance_declaration() -> Any:
    """Return the codex self-declaration for the maintenance domain.

    Fail-closed: raises ``RuntimeError`` when the declaration is absent
    from the Governance Codex, matching the previous module-level guard.
    """
    from governance_rule.execution.codex_official import official_self_declaration

    declaration = official_self_declaration("health-maintenance-test-sub-sovereign")
    if declaration is None:
        raise RuntimeError("health maintenance declaration not found in Governance Codex")
    return declaration


__all__ = ["maintenance_declaration"]
