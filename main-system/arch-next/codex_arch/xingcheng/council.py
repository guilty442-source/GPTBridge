"""xingcheng — 星澄（原生輔助系統）。

法典依據：P10 / A9 / A18 / A19 / A20 / A21 / E5 / E13 / E14。
  * 星澄為本地原生模型，與系統主宰同級之輔助系統（A18/E5）；
  * 可行權力僅限：觀察、分析、推理、建議、協調、解釋（A20/E14）；
  * 禁止直接執行、授權、覆寫任何決策或狀態（A21）；
  * 一切思考／管理引用本法典，不各自內建決策來源（A19/E13）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..shared.basis import DecisionBasis
from ..shared.contracts import SovereignRequest

POWERS: tuple[str, ...] = (
    "observe",
    "analyze",
    "reason",
    "advise",
    "coordinate",
    "explain",
)


@dataclass(frozen=True)
class Advisory:
    """星澄諮詢產出：唯讀，無狀態變更。"""

    power: str
    basis: tuple[str, ...]
    focus: str
    points: tuple[str, ...] = ()
    recommendation: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "power": self.power,
            "basis": list(self.basis),
            "focus": self.focus,
            "points": list(self.points),
            "recommendation": self.recommendation,
            "mutation": "none",
        }


class XingchengCouncil:
    """星澄議事會：對七主宰觀察並產出唯讀建議。"""

    def __init__(self, router: Any, *, authority: Any = None) -> None:
        self._router = router
        self._authority = authority

    # ---- A20 powers --------------------------------------------------

    def observe(self, sovereign_id: str) -> Advisory:
        outcome = self._router.route(
            sovereign_id,
            SovereignRequest(
                intent="status",
                subject=sovereign_id,
                requester="xingcheng",
            ),
        )
        return Advisory(
            power="observe",
            basis=("A20", "A18"),
            focus=f"status:{sovereign_id}",
            points=(
                ("accepted" if outcome.accepted else "refused",
                 f"intents:{sorted(outcome.basis)}" if outcome.accepted else f"refusal:{outcome.refusal.reason_code if outcome.refusal else 'none'}"),
            ),
        )

    def analyze(self, sovereign_ids: tuple[str, ...] = ()) -> Advisory:
        targets = sovereign_ids or self._router.ids()
        observations = []
        for sovereign_id in targets:
            result = self.observe(sovereign_id)
            observations.append(f"{sovereign_id}:{result.points[0][0]}")
        return Advisory(
            power="analyze",
            basis=("A20", "A21"),
            focus="sovereignty-snapshot",
            points=tuple(observations),
            recommendation="no-sovereign-action-required-if-all-accepted",
        )

    def reason(self, question: str) -> Advisory:
        return Advisory(
            power="reason",
            basis=("A19", "A20", "E13"),
            focus=question,
            points=("codex-referenced-reasoning-only",),
            recommendation="refer-to-codex-provisions",
        )

    def advise(self, subject: str, suggestion: str) -> Advisory:
        return Advisory(
            power="advise",
            basis=("A20", "E14"),
            focus=subject,
            points=(suggestion,),
            recommendation=suggestion,
        )

    def coordinate(self, plan: tuple[str, ...], *, between: tuple[str, ...] = ()) -> Advisory:
        """協調僅產出建議方案；不可自行施作（A21/A34）。"""
        if between and not set(between).issubset(self._router.ids()):
            return Advisory(
                power="coordinate",
                basis=("A20", "A21", "A34"),
                focus="cross-sovereign-coordination",
                points=("unknown-sovereign-in-scope",),
                recommendation="refuse-coordination-proposal",
            )
        return Advisory(
            power="coordinate",
            basis=("A20", "A21", "A34", "E14"),
            focus="cross-sovereign-coordination",
            points=plan,
            recommendation="submit-to-system-sovereign-for-orchestration",
        )

    def explain(self, provision: str) -> Advisory:
        try:
            explanation = f"{provision}-declared-in-codex"
        except Exception:  # pragma: no cover - defensive
            explanation = "provision-unknown"
        return Advisory(
            power="explain",
            basis=("A20", "E14"),
            focus=f"codex:{provision}",
            points=(explanation,),
            recommendation="reference-codex-only",
        )

    def status(self) -> dict[str, Any]:
        return {
            "powers": list(POWERS),
            "rank": "peer-of-system-sovereign",
            "exec_power": "none",
            "authority_present": self._authority is not None,
            "basis": list(DecisionBasis(("A18", "A20", "A21", "E14")).references),
        }


__all__ = ["Advisory", "POWERS", "XingchengCouncil"]