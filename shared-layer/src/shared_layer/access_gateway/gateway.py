from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Literal


Action = Literal["read", "create", "update", "delete", "manage", "execute"]


@dataclass(frozen=True)
class Principal:
    actor_id: str
    module_id: str
    is_xingcheng: bool = False


@dataclass(frozen=True)
class AccessDecision:
    allowed: bool
    reason: str


class AccessGateway:
    """Default-deny application gate; PostgreSQL RLS remains the final data gate."""

    def __init__(self, governance_authorizer: Callable[[Principal, Action, str], bool]) -> None:
        self._authorize = governance_authorizer

    def decide(
        self,
        principal: Principal,
        action: Action,
        target_module: str,
        *,
        resource_class: str = "data",
    ) -> AccessDecision:
        protected = str(resource_class or "").strip().casefold()
        if principal.is_xingcheng and protected in {
            "permission",
            "permission-file",
            "permission-directory",
            "governance-rule",
        } and action != "read":
            return AccessDecision(False, "PROTECTED_AUTHORITY_READ_ONLY")
        if principal.is_xingcheng and action == "execute":
            return AccessDecision(False, "XINGCHENG_NO_DIRECT_EXECUTION")
        if principal.is_xingcheng and action != "read" and target_module != principal.module_id:
            return AccessDecision(False, "XINGCHENG_CROSS_MODULE_READ_ONLY")
        if not principal.is_xingcheng and principal.module_id != target_module:
            if not self._authorize(principal, action, target_module):
                return AccessDecision(False, "CROSS_MODULE_DEFAULT_DENY")
        if not self._authorize(principal, action, target_module):
            return AccessDecision(False, "GOVERNANCE_DENY")
        return AccessDecision(True, "GOVERNANCE_ALLOW")


__all__ = ["AccessDecision", "AccessGateway", "Principal"]
