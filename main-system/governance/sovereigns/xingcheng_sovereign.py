"""Xingcheng Sovereign — 星澄主宰（獨立特權機構，完全擁有自有域，法典決策鏈外）。

法典依據:
- sovereign_id: 星澄 (position 5)
- area: xingcheng
- rank: independent-privileged-institution
- basis: codex
- duties: observe+analyze+reason+decide+manage+authorize+execute+write+delete+configure (in owned domain)
- powers: complete-inside-owned-domain
- prohibitions: FORBID:any-星澄-power-outside-owned-domain; FORBID:any-system-target-or-effect (A20)

A12: 星澄在決策鏈外
A20: 星澄權力完整在自有域內，禁止任何系統目標或效果

Auxiliary review group (separate+non-transitive from own-domain power):
- A137: authenticated read-only evidence projection is the only system interface.
- A138: independent analysis+reasoning over governed global evidence; results advisory-only + traceable.
- A139: 星澄-SYSTEM-POWER: global-read-only-review + anomaly-classification + user-notification.
- A140: information layer is the sole provider of filtered system review evidence.
- A144/A174: 星澄 is the sole auxiliary codex-view exception — official entry, read-only.
- A145: auxiliary duties = reference codex as basis + review all system domains + notify user.
- Language-review capability: transferred to 星澄 when language-review-sub-sovereign abolished (A334).

Special-law powers (basis A313|A328|A336):
- A313: all permission-review authority transferred to 星澄.
- A328: free-entry privilege — confidential read-only inspection of every governed layer.
- A336: abolished system-programming + language-review sub-sovereign duties transfer to 星澄.
  Programming analysis/design/code authoring/refactoring/debugging/tool use/verification and
  language conformance are exclusive native-model capabilities.
  Whole-system automation coordination executor exclusively vested in 星澄.

Full-automation upgrade (A20):
- Background auto-loop observes, analyzes, reasons, manages the owned domain automatically.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
from pathlib import Path
from typing import Any

from ._base import SovereignBase, SovereignOutcome, SovereignRequest
from core_system.codex_decision import accepted_outcome, refusal_outcome

# Sub-modules
from .xingcheng.review import XingchengReviewMixin
from .xingcheng.native_capability import XingchengNativeMixin
from .xingcheng.domain import XingchengDomainMixin
from .xingcheng.auto import XingchengAutoMixin

_logger = logging.getLogger("gptbridge.sovereign.xingcheng")

# Owned-domain root
_OWNED_DOMAIN_ROOT = "Standalone tools/local-model/model-dialogue/xingcheng"

# Domain health thresholds
_DB_MAX_SIZE_BYTES = 500 * 1024 * 1024
_MODEL_DIR_MAX_SIZE_BYTES = 50 * 1024 * 1024 * 1024
_CHECKPOINT_MAX_AGE_HOURS = 72


class XingchengSovereign(
    SovereignBase,
    XingchengReviewMixin,
    XingchengNativeMixin,
    XingchengDomainMixin,
    XingchengAutoMixin,
):
    """星澄主宰：自有域完全權力，隔離於系統決策鏈。"""

    sovereign_id = "星澄"

    # A10/A11 explicit intent allowlist — adjudicated intents explicitly
    _INTENT_ALLOWLIST: frozenset[str] = frozenset({
        # Owned-domain powers (A20)
        "domain.observe", "domain.analyze", "domain.reason", "domain.decide",
        "domain.manage", "domain.authorize", "domain.execute", "domain.write",
        "domain.delete", "domain.configure", "channel.coordinate",
        # Auto-automation intents (A20 owned-domain only)
        "domain.auto-observe", "domain.auto-analyze", "domain.auto-manage",
        "domain.auto-health-check",
        # Auxiliary review group (A137-A146, separate+non-transitive)
        "review.global", "review.classify-anomaly", "review.notify-user",
        "review.language", "codex.read", "inspect.layer",
        # A313: permission-review authority transferred to 星澄
        "review.permission",
        # A328: free-entry confidential read-only inspection
        "inspect.layer",
        # A336: automatic classification (auxiliary + native model)
        "classify.intent", "classify.task", "classify.code", "classify.fault",
        "classify.evidence", "classify.result",
        # A336: star adjudication
        "star.adjudicate-classification",
        # A336: native-model programming capabilities
        "program.analyze", "program.design", "program.write", "program.refactor",
        "program.debug", "program.verify", "program.migrate", "program.review-generated",
        # A336: whole-system automation coordination executor
        "automation.decompose", "automation.schedule", "automation.dispatch",
        "automation.converge", "automation.verify-result", "automation.contain-failure",
    })

    def __init__(self, app: Any | None = None) -> None:
        super().__init__(app)
        app_root = getattr(self.app, "project_root", None)
        self._project_root = (
            Path(app_root).resolve()
            if app_root
            else Path(__file__).resolve().parents[3]
        )
        self._owned_domain_root = (
            self._project_root / _OWNED_DOMAIN_ROOT
        ).as_posix()
        self._isolated = True

        # Initialize mixin states
        XingchengReviewMixin.__init__(self)
        XingchengNativeMixin.__init__(self)
        XingchengDomainMixin.__init__(self)
        XingchengAutoMixin.__init__(self)

    def _verify_intent(self, intent: str) -> bool:
        """A10/A11 fail-closed: only declared intents pass."""
        return intent in self._INTENT_ALLOWLIST

    async def _adjudicate(self, request: SovereignRequest) -> SovereignOutcome:
        """裁決：自有域完全權力 + 輔助唯讀審查（兩群分立、不可遞移）。"""
        intent = request.intent

        # Auxiliary + special-law groups (A137-A146/A313/A328/A336):
        # read-only/advisory, no system effects
        if (
            intent.startswith("review.")
            or intent == "codex.read"
            or intent.startswith("inspect.")
            or intent.startswith("classify.")
            or intent == "star.adjudicate-classification"
        ):
            return await self.adjudicate_review(request)

        # A336 native-model programming + whole-system automation executor
        if intent.startswith("program.") or intent.startswith("automation."):
            return await self.adjudicate_native_capability(request)

        # Owned-domain gate (A20)
        if not self._is_in_owned_domain(request):
            return refusal_outcome(
                "OUTSIDE_OWNED_DOMAIN", self.verified_basis("A20", "A12"),
            )

        # Owned-domain power intents
        if intent == "domain.observe":
            return await self._adjudicate_observe(request)
        if intent == "domain.analyze":
            return await self._adjudicate_analyze(request)
        if intent == "domain.reason":
            return await self._adjudicate_reason(request)
        if intent == "domain.decide":
            return await self._adjudicate_decide(request)
        if intent == "domain.manage":
            return await self._adjudicate_manage(request)
        if intent == "domain.authorize":
            return await self._adjudicate_authorize(request)
        if intent == "domain.execute":
            return await self._adjudicate_execute(request)
        if intent == "domain.write":
            return await self._adjudicate_write(request)
        if intent == "domain.delete":
            return await self._adjudicate_delete(request)
        if intent == "domain.configure":
            return await self._adjudicate_configure(request)
        if intent == "channel.coordinate":
            return await self._adjudicate_channel_coordinate(request)
        if intent == "domain.auto-observe":
            return await self._adjudicate_auto_observe(request)
        if intent == "domain.auto-analyze":
            return await self._adjudicate_auto_analyze(request)
        if intent == "domain.auto-manage":
            return await self._adjudicate_auto_manage(request)
        if intent == "domain.auto-health-check":
            return await self._adjudicate_auto_health_check(request)

        return refusal_outcome("UNKNOWN_INTENT", self.verified_basis("A20", "A12"))

    async def _delegate_execution(
        self, decision: SovereignOutcome, request: SovereignRequest
    ) -> SovereignOutcome:
        """星澄委派執行（A69/A121）。"""
        return decision

    async def start(self) -> dict[str, Any]:
        """Start the sovereign and auto-loop."""
        await super().start()
        await self.start_auto_loop()
        return {
            "ok": True,
            "role": self.sovereign_id,
            "started_at": self._iso_now(),
            "owned_domain": self._owned_domain_root,
            "auto_loop": "started",
        }

    async def stop(self) -> None:
        """Stop the sovereign and auto-loop."""
        await self.stop_auto_loop()
        self._started = False

    def live_status(self) -> dict[str, Any]:
        return {
            "role": self.sovereign_id,
            "area": "xingcheng",
            "rank": "independent-privileged-institution",
            "started": self._started,
            "isolated": self._isolated,
            "owned_domain": self._owned_domain_root,
            "auto_loop": self.auto_status(),
            "reviews": len(self._reviews),
            "program_tasks": len(self._program_tasks),
            "automation_tasks": len(self._automation_tasks),
            "last_snapshot": self._last_snapshot,
            "pending_anomalies": len(self._pending_anomalies),
        }

    def orchestration_status(self) -> dict[str, Any]:
        return {
            "name": "xingcheng",
            "role": self.sovereign_id,
            "area": "xingcheng",
            "state": "running" if self._started else "stopped",
            "powers": "complete-inside-owned-domain",
            "prohibitions": "no-system-targets",
            "auto_loop": self.auto_status(),
        }