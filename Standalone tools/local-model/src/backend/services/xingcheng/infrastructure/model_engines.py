from __future__ import annotations

from typing import Any, Callable, Mapping, Sequence

from ..domain.model_registry import StarModelProfile, StarModelRegistry
from .generative_language_model import FIRST_PARTY_CORPUS
from .native_model import StarNativeLanguageModel


class IsolatedNativeModelEngine:
    """One role-bound native engine; engines never share mutable runtime state."""

    VERSION = StarNativeLanguageModel.VERSION
    ARCHITECTURE = StarNativeLanguageModel.ARCHITECTURE

    def __init__(
        self,
        profile: StarModelProfile,
        *,
        allowed_intents: frozenset[str],
        learned_examples: Sequence[Mapping[str, Any]] = (),
    ) -> None:
        self.profile = profile
        self.allowed_intents = allowed_intents
        role_corpus = {
            intent: texts
            for intent, texts in FIRST_PARTY_CORPUS.items()
            if intent in allowed_intents or intent == "capabilities"
        }
        self.runtime = StarNativeLanguageModel(
            learned_examples=learned_examples,
            corpus=role_corpus,
            model_role=profile.role,
        )

    def classify_intents(self, prompt: str) -> list[str]:
        return self.runtime.classify_intents(prompt)

    def semantic_plan(
        self,
        prompt: str,
        *,
        context: str = "",
        confirmed: bool = False,
    ) -> dict[str, Any]:
        return self.runtime.semantic_plan(
            prompt,
            context=context,
            confirmed=confirmed,
        )

    def infer(
        self,
        payload: dict[str, Any],
        *,
        database: dict[str, Any],
        analyze: Callable[[dict[str, Any]], dict[str, Any]],
        search: Callable[[dict[str, Any]], dict[str, Any]],
    ) -> dict[str, Any]:
        inferred_intent = str(
            payload.get("_governed_intent")
            or self.runtime.classify_intent(
                str(payload.get("instruction") or payload.get("prompt") or "")
            )
        )
        if inferred_intent not in self.allowed_intents:
            return {
                "ok": False,
                "error_code": "MODEL_CAPABILITY_BOUNDARY",
                "intent": inferred_intent,
                "model": self.profile.model_id,
            }
        result = self.runtime.infer(
            payload,
            database=database,
            analyze=analyze,
            search=search,
        )
        result["engine_model_id"] = self.profile.model_id
        result["engine_isolated"] = True
        return result

    def learn_verified_example(self, example: Mapping[str, Any]) -> bool:
        return self.runtime.learn_verified_example(example)

    def training_status(self) -> dict[str, Any]:
        return self.runtime.training_status()


class StarModelEngines:
    """Own role-isolated engines and expose only main-model routing."""

    _MAIN_INTENTS = frozenset(
        {
            "conversation",
            "capabilities",
            "status",
            "search",
            "distribution",
            "quote",
            "risk",
            "analysis",
            "calculation",
            "reasoning",
            "statistics",
            "data_organization",
            "coding",
            "self_upgrade",
            "reading",
        }
    )

    def __init__(
        self,
        registry: StarModelRegistry,
        *,
        learned_examples_by_model: Mapping[
            str, Sequence[Mapping[str, Any]]
        ] | None = None,
    ) -> None:
        self.registry = registry
        examples = learned_examples_by_model or {}
        self._engines = {
            registry.MAIN.model_id: IsolatedNativeModelEngine(
                registry.MAIN,
                allowed_intents=self._MAIN_INTENTS,
                learned_examples=examples.get(registry.MAIN.model_id, ()),
            ),
            registry.INVESTMENT.model_id: IsolatedNativeModelEngine(
                registry.INVESTMENT,
                allowed_intents=registry.investment_intents,
                learned_examples=examples.get(registry.INVESTMENT.model_id, ()),
            ),
            registry.MATHEMATICAL.model_id: IsolatedNativeModelEngine(
                registry.MATHEMATICAL,
                allowed_intents=registry.mathematical_intents,
                learned_examples=examples.get(registry.MATHEMATICAL.model_id, ()),
            ),
            registry.CODING.model_id: IsolatedNativeModelEngine(
                registry.CODING,
                allowed_intents=registry.coding_intents,
                learned_examples=examples.get(registry.CODING.model_id, ()),
            ),
        }

    @property
    def main(self) -> IsolatedNativeModelEngine:
        return self._engines[self.registry.MAIN.model_id]

    def for_profile(self, profile: StarModelProfile) -> IsolatedNativeModelEngine:
        self.registry.authorize_delegation(self.registry.primary.model_id, profile)
        try:
            return self._engines[profile.model_id]
        except KeyError as error:
            raise PermissionError("UNKNOWN_MODEL_ISOLATION_DENIED") from error

    def status(self) -> dict[str, dict[str, object]]:
        return {
            model_id: {
                "model_id": model_id,
                "role": engine.profile.role,
                "runtime_instance": f"isolated:{model_id}",
                "network_policy": engine.profile.network_policy,
                "allowed_intents": sorted(engine.allowed_intents),
                "language_model": engine.training_status(),
            }
            for model_id, engine in self._engines.items()
        }


__all__ = ["IsolatedNativeModelEngine", "StarModelEngines"]
