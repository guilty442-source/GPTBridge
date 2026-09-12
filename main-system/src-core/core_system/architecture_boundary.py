"""Architecture boundary enforcement for four-module isolation (A276-A284).

Per Governance Codex:

A276: main-system submits to governance authority
A277: local-model independent lifecycle, no main-system failure propagation
A278: local-model isolated, no cross-tool source/data/repair
A279: codex independent, read-only, sole authority
A280: Four modules - main-system, shared-layer, local-model, governance_rule
A281: Git worktree governance with coordinator-only push
A282: Launcher wakes main-system only, no governance
A283: Shared-layer serves main-system, no governance
A284: Local-model tools isolated, own data authority

This module provides runtime boundary enforcement.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Final

_logger = logging.getLogger("gptbridge.architecture_boundary")


class ModuleIdentity(Enum):
    """The four architectural modules (A280)."""
    MAIN_SYSTEM = "main-system"
    SHARED_LAYER = "shared-layer"
    LOCAL_MODEL = "local-model"
    GOVERNANCE_RULE = "governance_rule"


MODULE_ROOTS: Final[dict[ModuleIdentity, str]] = {
    ModuleIdentity.MAIN_SYSTEM: "main-system",
    ModuleIdentity.SHARED_LAYER: "shared-layer",
    ModuleIdentity.LOCAL_MODEL: "local-model",
    ModuleIdentity.GOVERNANCE_RULE: "governance_rule",
}

MODULE_AUTHORITIES: Final[dict[ModuleIdentity, frozenset[str]]] = {
    ModuleIdentity.MAIN_SYSTEM: frozenset({
        "governance-authority",
        "startup-sovereign",
        "system-runtime-sovereign",
        "system-programming-sovereign",
        "permission-sovereign",
        "maintenance-sovereign",
    }),
    ModuleIdentity.SHARED_LAYER: frozenset({
        "information-layer",
        "channel-runtime",
        "postgresql-pool",
        "qdrant-pool",
    }),
    ModuleIdentity.LOCAL_MODEL: frozenset({
        "independent-tool-lifecycle",
        "own-data-authority",
        "own-source-boundary",
        "own-process-tree",
        "own-window-host",
        "own-runtime-generation",
        "own-repair-isolation",
    }),
    ModuleIdentity.GOVERNANCE_RULE: frozenset({
        "codex-authority",
        "read-only",
        "sole-authority",
    }),
}


@dataclass(frozen=True)
class BoundaryRule:
    """A boundary rule between modules."""
    from_module: ModuleIdentity
    to_module: ModuleIdentity
    allowed: bool
    description: str
    codex_reference: str


# Boundary rules per A276-A284
BOUNDARY_RULES: Final[tuple[BoundaryRule, ...]] = (
    # Launcher -> main-system (A282)
    BoundaryRule(
        from_module=ModuleIdentity.LOCAL_MODEL,  # Launcher is separate
        to_module=ModuleIdentity.MAIN_SYSTEM,
        allowed=True,
        description="Launcher wakes main-system only, no governance",
        codex_reference="A282",
    ),
    # main-system -> governance_rule (A276)
    BoundaryRule(
        from_module=ModuleIdentity.MAIN_SYSTEM,
        to_module=ModuleIdentity.GOVERNANCE_RULE,
        allowed=True,
        description="main-system submits to governance authority",
        codex_reference="A276",
    ),
    # shared-layer -> main-system (A283)
    BoundaryRule(
        from_module=ModuleIdentity.SHARED_LAYER,
        to_module=ModuleIdentity.MAIN_SYSTEM,
        allowed=True,
        description="shared-layer serves main-system, no governance",
        codex_reference="A283",
    ),
    # local-model -> main-system (A277, A278)
    BoundaryRule(
        from_module=ModuleIdentity.LOCAL_MODEL,
        to_module=ModuleIdentity.MAIN_SYSTEM,
        allowed=False,
        description="local-model isolated, no main-system failure propagation",
        codex_reference="A277,A278",
    ),
    # main-system -> local-model (A277, A278)
    BoundaryRule(
        from_module=ModuleIdentity.MAIN_SYSTEM,
        to_module=ModuleIdentity.LOCAL_MODEL,
        allowed=True,
        description="main-system can launch local-model tools",
        codex_reference="A277,A278",
    ),
    # governance_rule -> any (A279 - read-only)
    BoundaryRule(
        from_module=ModuleIdentity.GOVERNANCE_RULE,
        to_module=ModuleIdentity.MAIN_SYSTEM,
        allowed=False,
        description="codex independent, read-only, no execution",
        codex_reference="A279",
    ),
    BoundaryRule(
        from_module=ModuleIdentity.GOVERNANCE_RULE,
        to_module=ModuleIdentity.SHARED_LAYER,
        allowed=False,
        description="codex independent, read-only, no execution",
        codex_reference="A279",
    ),
    BoundaryRule(
        from_module=ModuleIdentity.GOVERNANCE_RULE,
        to_module=ModuleIdentity.LOCAL_MODEL,
        allowed=False,
        description="codex independent, read-only, no execution",
        codex_reference="A279",
    ),
    # shared-layer -> governance_rule (A283)
    BoundaryRule(
        from_module=ModuleIdentity.SHARED_LAYER,
        to_module=ModuleIdentity.GOVERNANCE_RULE,
        allowed=False,
        description="shared-layer serves main-system, no governance",
        codex_reference="A283",
    ),
    # local-model -> shared-layer (A278)
    BoundaryRule(
        from_module=ModuleIdentity.LOCAL_MODEL,
        to_module=ModuleIdentity.SHARED_LAYER,
        allowed=True,
        description="local-model tools use shared-layer information layer",
        codex_reference="A278",
    ),
    # local-model -> governance_rule (A278)
    BoundaryRule(
        from_module=ModuleIdentity.LOCAL_MODEL,
        to_module=ModuleIdentity.GOVERNANCE_RULE,
        allowed=True,
        description="local-model tools submit to governance via main-system",
        codex_reference="A278",
    ),
)


def resolve_module_identity(path: Path) -> ModuleIdentity | None:
    """Resolve the module identity from a file path."""
    parts = path.resolve().parts
    for part in parts:
        for module in ModuleIdentity:
            if part == MODULE_ROOTS[module]:
                return module
    return None


def check_boundary(
    from_path: Path,
    to_path: Path,
    action: str = "access",
) -> tuple[bool, str]:
    """Check if an action across module boundaries is allowed.

    Returns (allowed, reason).
    """
    from_module = resolve_module_identity(from_path)
    to_module = resolve_module_identity(to_path)

    if from_module is None or to_module is None:
        return True, "outside-known-modules"

    if from_module == to_module:
        return True, "same-module"

    # Check boundary rules
    for rule in BOUNDARY_RULES:
        if rule.from_module == from_module and rule.to_module == to_module:
            return rule.allowed, f"{rule.description} ({rule.codex_reference})"

    # Default deny for unknown boundaries
    return False, f"no-rule-for-{from_module.value}->{to_module.value}"


def enforce_boundary(
    from_path: Path,
    to_path: Path,
    action: str = "access",
) -> None:
    """Enforce a boundary - raises if not allowed."""
    allowed, reason = check_boundary(from_path, to_path, action)
    if not allowed:
        raise PermissionError(
            f"Boundary violation: {from_path} -> {to_path} ({action}): {reason}"
        )
    _logger.debug("boundary_ok %s -> %s (%s): %s", from_path, to_path, action, reason)


def get_module_authorities(module: ModuleIdentity) -> frozenset[str]:
    """Get the authorities for a module."""
    return MODULE_AUTHORITIES.get(module, frozenset())


def validate_module_identity(module: ModuleIdentity, expected_authorities: set[str]) -> bool:
    """Validate that a module has the expected authorities."""
    actual = MODULE_AUTHORITIES.get(module, frozenset())
    return expected_authorities.issubset(actual)


__all__ = [
    "ModuleIdentity",
    "MODULE_ROOTS",
    "MODULE_AUTHORITIES",
    "BoundaryRule",
    "BOUNDARY_RULES",
    "resolve_module_identity",
    "check_boundary",
    "enforce_boundary",
    "get_module_authorities",
    "validate_module_identity",
]