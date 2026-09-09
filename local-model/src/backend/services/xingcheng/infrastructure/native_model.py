from __future__ import annotations

from typing import Any, Callable, Iterable, Mapping, Sequence

from .generative_language_model import StarAutoregressiveLanguageModel
from .native_constants import StarNativeConstantsMixin
from .native_intent import StarNativeIntentMixin
from .native_normalization import StarNativeNormalizationMixin
from .native_comprehension import StarNativeComprehensionMixin
from .native_safety import StarNativeSafetyMixin
from .native_plan import StarNativePlanMixin
from .native_grounding import StarNativeGroundingMixin
from .native_inference import StarNativeInferenceMixin


InvestmentAnalyzer = Callable[[dict[str, Any]], dict[str, Any]]
MarketSearcher = Callable[[dict[str, Any]], dict[str, Any]]


class StarNativeLanguageModel(
    StarNativeConstantsMixin,
    StarNativeIntentMixin,
    StarNativeNormalizationMixin,
    StarNativeComprehensionMixin,
    StarNativeSafetyMixin,
    StarNativePlanMixin,
    StarNativeGroundingMixin,
    StarNativeInferenceMixin,
):
    """First-party, locally trained language-model and governed tool pipeline."""

    def __init__(
        self,
        *,
        learned_examples: Iterable[Mapping[str, Any]] = (),
        corpus: Mapping[str, Sequence[str]] | None = None,
        model_role: str = "main",
    ) -> None:
        self.model_role = str(model_role or "main")
        self.language_model = StarAutoregressiveLanguageModel(
            learned_examples=learned_examples,
            corpus=corpus,
        )

    def training_status(self) -> dict[str, Any]:
        return {
            **self.language_model.metrics(),
            "model_role": self.model_role,
            "training_mode": "continuous-verified-self-distillation-and-ollama-local-model-training",
            "training_data_scope": "star-owned-and-star-validated-gpt-candidates",
            "quality_gate_required": True,
            "rollback_source": "versioned-training-examples",
            "gpt_candidate_direct_write": False,
            "gpt_weight_access": False,
        }

    def learn_verified_example(self, example: Mapping[str, Any]) -> bool:
        if example.get("validated") is not True:
            return False
        return self.language_model.learn(
            str(example.get("intent") or "capabilities"),
            str(example.get("target_text") or ""),
            input_text=str(example.get("input_text") or ""),
            weight=max(
                1,
                min(5, round(float(example.get("quality_score") or 0.8) * 5)),
            ),
            source="self-training",
        )


__all__ = ["StarNativeLanguageModel"]
