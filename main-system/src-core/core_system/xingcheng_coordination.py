"""Xingcheng coordination — wires the Xingcheng auxiliary system into the
System Sovereign.

Xingcheng (module ``xingcheng``) is a LOCAL NATIVE MODEL and an auxiliary
system PEERED with the System Sovereign (same tier).  It has NO execution
authority and THINKS by referencing the Governance Codex:

  * native model          = xingcheng native/Transformer runtime
  * rank                  = peer to the System Sovereign (auxiliary, same tier)
  * decision & thinking   = referenced from the Governance Codex
  * execution             = false (no execution authority)
  * read-only management  = true
  * git_write / sql_write / rag_mutation = false

The sovereign coordinates Xingcheng at the decision level only.  This module is
lightweight (no heavy AI/model import) and reads Xingcheng's residency from the
mother app's governed-tool state, so the mother process never runs Xingcheng's
execution stack in-process (execution_delegation = governed-executor-only).
"""

from __future__ import annotations

from typing import Any

from governance_rule.codex import GOVERNANCE_CODEX

from .codex_decision import decision_basis
from .versioning import application_version


_XINGCHENG_SOVEREIGN = next(
    (s for s in GOVERNANCE_CODEX.sovereigns if s.area == "xingcheng"),
    None,
)
if _XINGCHENG_SOVEREIGN is None:
    raise RuntimeError("xingcheng sovereign not found in Governance Codex")

XINGCHENG_ROLE = _XINGCHENG_SOVEREIGN.id
XINGCHENG_MODULE_ID = _XINGCHENG_SOVEREIGN.id
XINGCHENG_MODE = "intelligent-management"
XINGCHENG_RANK = _XINGCHENG_SOVEREIGN.rank
XINGCHENG_KIND = "local-native-model"

XINGCHENG_EMPOWERED_POWERS = _XINGCHENG_SOVEREIGN.powers
XINGCHENG_PROHIBITED_POWERS = _XINGCHENG_SOVEREIGN.prohibitions


class XingchengCoordination:
    """Decision-level coordinates for the Xingcheng auxiliary system.

    Every decision and thinking references the Governance Codex (areas
    ``xingcheng`` / ``xingcheng-thinking``); this agent does not own its
    decision source.  Xingcheng never executes.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    # ------------------------------------------------------------------
    # Coordination surface
    # ------------------------------------------------------------------

    def coordination_status(self) -> dict[str, Any]:
        """Coordinated snapshot of Xingcheng's auxiliary system presence."""

        app_version = application_version(self.app.project_root)
        delegated, tool_state = self._xingcheng_tool_state()

        return {
            "module_id": XINGCHENG_MODULE_ID,
            "role": XINGCHENG_ROLE,
            "rank": XINGCHENG_RANK,
            "kind": XINGCHENG_KIND,
            "mode": XINGCHENG_MODE,
            "authority": {
                "native_model": True,
                "decision_and_orchestration": True,
                "read_only_management": True,
                "execution": False,
                "git_write": False,
                "sql_write": False,
                "rag_mutation": False,
            },
            "powers": {
                "empowered": list(XINGCHENG_EMPOWERED_POWERS),
                "prohibited": list(XINGCHENG_PROHIBITED_POWERS),
            },
            "version": app_version,
            "delegated": delegated,
            "tool_state": tool_state,
            "delegation": "governed-executor-only",
            "thinking": decision_basis("xingcheng-thinking"),
            "decision": decision_basis("xingcheng"),
        }

    def orchestration_status(self) -> dict[str, Any]:
        """Unified subsystem view used by the sovereign's orchestration report."""

        delegated, tool_state = self._xingcheng_tool_state()
        resident = bool(tool_state.get("resident_service")) if tool_state else False
        state = "delegated" if delegated else ("resident" if resident else "idle")
        return {
            "name": "xingcheng",
            "module_id": XINGCHENG_MODULE_ID,
            "role": XINGCHENG_ROLE,
            "rank": XINGCHENG_RANK,
            "kind": XINGCHENG_KIND,
            "state": state,
            "delegation": "governed-executor-only",
            "authority": "intelligent-management",
            "execution": False,
            "powers": {
                "empowered": list(XINGCHENG_EMPOWERED_POWERS),
                "prohibited": list(XINGCHENG_PROHIBITED_POWERS),
            },
            "thinking": decision_basis("xingcheng-thinking"),
            "decision": decision_basis("xingcheng"),
        }

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _xingcheng_tool_state(self) -> tuple[bool, dict[str, Any] | None]:
        """Locate Xingcheng (xingcheng) residency in the mother app's state.

        Returns ``(delegated, tool_state)``; ``delegated`` is True when the
        governed tool reports being handed to an executor.
        """

        toolbox = getattr(self.app, "toolbox_service", None)
        try:
            status = toolbox.status() if hasattr(toolbox, "status") else {}
        except Exception:
            status = {}

        tools = status.get("tools") or {}
        tool = None
        if isinstance(tools, dict):
            tool = tools.get(XINGCHENG_MODULE_ID)
        elif isinstance(tools, list):
            for item in tools:
                if isinstance(item, dict) and item.get("tool_id") == XINGCHENG_MODULE_ID:
                    tool = item
                    break

        if not isinstance(tool, dict):
            # Fall back to the default-tool startup record.
            fallback = getattr(self.app, "default_tool_startup", {})
            record = fallback.get(XINGCHENG_MODULE_ID)
            if isinstance(record, dict):
                delegated = bool(record.get("ok") is True)
                return delegated, {"resident_service": delegated, "startup": record}
            return False, None

        delegated = bool(
            tool.get("delegated") or tool.get("executor_state") or tool.get("ok")
        )
        return delegated, tool


__all__ = [
    "XINGCHENG_MODULE_ID",
    "XINGCHENG_MODE",
    "XINGCHENG_ROLE",
    "XingchengCoordination",
]
