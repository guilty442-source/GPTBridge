"""Internal Capability Base — 主權內建能力底座。

A592/A604: the sub-sovereign layer is eliminated and 星澄 (the native
model own-domain sovereign) has no module/child concept — owned
capabilities are internal components of the sovereign itself.  An
internal capability:

* is instantiated and driven in-process by its owning sovereign,
* is never registered in a module/child registry and never reachable
  through sovereign routing or ``delegate_to``,
* has no external request surface — the single entry gate fails closed,
* acts under the owner's own codex identity (``sovereign_id``).
"""

from __future__ import annotations

from abc import ABC
from typing import Any

from .._base import SovereignBase, SovereignOutcome, SovereignRequest
from core_system.codex_decision import refusal_outcome


class InternalCapabilityBase(SovereignBase, ABC):
    """內建能力底座：由擁有核心於行程內直接驅動，無對外請求面。"""

    async def _adjudicate(self, request: SovereignRequest) -> SovereignOutcome:
        """Fail closed: internal capabilities have no external request surface."""
        return refusal_outcome(
            "INTERNAL_CAPABILITY_NO_EXTERNAL_SURFACE",
            self.verified_basis("A592", "A604"),
        )

    async def run_command(
        self, intent: str, payload: dict[str, Any] | None = None
    ) -> SovereignOutcome:
        """Owner-driven in-process command dispatch.

        The owning sovereign issues bounded commands directly; no
        delegation receipt, nonce or parent-authorization gate is needed
        because the caller is the owner itself.
        """
        return await self._run_command(
            SovereignRequest(
                intent=intent,
                subject="internal-capability",
                requester=self.sovereign_id,
                payload=dict(payload or {}),
            )
        )

    async def _run_command(self, request: SovereignRequest) -> SovereignOutcome:
        raise NotImplementedError


__all__ = ["InternalCapabilityBase"]
