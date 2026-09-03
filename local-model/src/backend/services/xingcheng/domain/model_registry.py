from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class StarModelProfile:
    model_id: str
    name: str
    role: str
    database_scope: str
    network_policy: str
    external_collaboration: str

    def status(self, *, primary: bool = False) -> dict[str, Any]:
        return {**asdict(self), "primary": primary, "enabled": True}


class StarModelRegistry:
    """Governed model-role registry behind Star's single AI channel."""

    ROUTING_MODE = "automatic-main-model-orchestration"

    MAIN = StarModelProfile(
        model_id="star-main-native-model",
        name="主要日常模型",
        role="daily-primary",
        database_scope="main",
        network_policy="public-web-read-only",
        external_collaboration="disabled",
    )
    INVESTMENT = StarModelProfile(
        model_id="star-investment-native-model",
        name="投資專家",
        role="investment-specialist",
        database_scope="investment",
        network_policy="disabled",
        external_collaboration="disabled",
    )
    MATHEMATICAL = StarModelProfile(
        model_id="star-mathematical-native-model",
        name="數理專家",
        role="mathematical-reasoning-specialist",
        database_scope="mathematical",
        network_policy="disabled",
        external_collaboration="disabled",
    )
    CODING = StarModelProfile(
        model_id="star-coding-native-model",
        name="程式設計專家",
        role="coding-specialist",
        database_scope="coding",
        network_policy="disabled",
        external_collaboration="disabled",
    )
    _INVESTMENT_INTENTS = frozenset({"distribution", "quote", "risk", "analysis"})
    _MATHEMATICAL_INTENTS = frozenset(
        {"calculation", "reasoning", "statistics", "data_organization"}
    )
    _CODING_INTENTS = frozenset({"coding", "self_upgrade"})

    @property
    def primary(self) -> StarModelProfile:
        return self.MAIN

    @property
    def profiles(self) -> tuple[StarModelProfile, ...]:
        return (self.MAIN, self.INVESTMENT, self.MATHEMATICAL, self.CODING)

    @property
    def investment_intents(self) -> frozenset[str]:
        return self._INVESTMENT_INTENTS

    @property
    def mathematical_intents(self) -> frozenset[str]:
        return self._MATHEMATICAL_INTENTS

    @property
    def coding_intents(self) -> frozenset[str]:
        return self._CODING_INTENTS

    def for_command(
        self, command: str, *, intent: str = ""
    ) -> StarModelProfile:
        """Select a model automatically; callers cannot choose a specialist."""
        if intent in self._MATHEMATICAL_INTENTS:
            return self.MATHEMATICAL
        if intent in self._CODING_INTENTS:
            return self.CODING
        if command == "xingcheng_search_investments":
            return self.MAIN
        if command == "xingcheng_analyze_investments" or intent in self._INVESTMENT_INTENTS:
            return self.INVESTMENT
        return self.MAIN

    def catalog(self) -> list[dict[str, Any]]:
        return [
            {
                **profile.status(primary=profile == self.primary),
                "selection_mode": "automatic",
                "direct_selection": False,
            }
            for profile in self.profiles
        ]

    def arrange_tasks(self, intents: list[str]) -> list[dict[str, Any]]:
        """Build an ordered, main-model-owned plan for all detected work."""

        normalized = list(dict.fromkeys(str(item).strip() for item in intents if item))
        if not normalized:
            normalized = ["capabilities"]
        return [
            {
                "sequence": index,
                "intent": intent,
                "assigned_model": self.for_command("xingcheng_infer", intent=intent).model_id,
                "coordinator_model": self.primary.model_id,
                "fallback_model": self.primary.model_id,
                "selection": "automatic",
                "governance_required": True,
            }
            for index, intent in enumerate(normalized, start=1)
        ]

    def authorize_delegation(
        self,
        coordinator_model: str,
        target: StarModelProfile,
    ) -> None:
        if coordinator_model != self.primary.model_id:
            raise PermissionError("MODEL_DELEGATION_ISOLATION_DENIED")
        if target not in self.profiles:
            raise PermissionError("UNKNOWN_MODEL_ISOLATION_DENIED")
