"""Governance Rules Management.

Immutable governance rules catalog loading and normalization.
"""

from __future__ import annotations

from typing import Any

from governance_rule.governance_policy import (
    DEFAULT_ACTIVE_GOVERNANCE_RULES,
    GOVERNANCE_RULE_CATALOG,
)


class GovernanceRulesManager:
    """Manages immutable governance rules catalog."""

    AVAILABLE_GOVERNANCE_RULES = list(GOVERNANCE_RULE_CATALOG)

    def __init__(self) -> None:
        self._rules = self._load_governance_rules()
        self._read_only = True

    def _load_governance_rules(self) -> list[str]:
        """Return the versioned, immutable main-system governance catalog."""
        return list(DEFAULT_ACTIVE_GOVERNANCE_RULES)

    def get_rules(self) -> list[str]:
        """Get current governance rules."""
        return list(self._rules)

    def normalize(self, rules: Any) -> list[str]:
        """Normalize incoming rules against catalog."""
        catalog = [str(item).strip() for item in self.AVAILABLE_GOVERNANCE_RULES]
        incoming = rules if isinstance(rules, list) else []
        normalized = [str(item).strip() for item in incoming if str(item).strip()]
        return list(dict.fromkeys([*catalog, *normalized]))

    def save(self) -> None:
        """Save rules - not allowed at runtime."""
        raise PermissionError("Governance rules are immutable at runtime")

    @property
    def is_read_only(self) -> bool:
        return True

    @property
    def rules(self) -> list[str]:
        return list(self._rules)


__all__ = ["GovernanceRulesManager"]