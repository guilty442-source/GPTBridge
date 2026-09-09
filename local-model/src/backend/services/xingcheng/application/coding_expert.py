from __future__ import annotations

from .coding_expert_constants import CodingExpertConstants
from .coding_expert_spec import CodingExpertSpecMixin
from .coding_expert_generation import CodingExpertGenerationMixin
from .coding_expert_analysis import CodingExpertAnalysisMixin
from .coding_expert_process import CodingExpertProcessMixin


class StarCodingExpert(
    CodingExpertConstants,
    CodingExpertSpecMixin,
    CodingExpertGenerationMixin,
    CodingExpertAnalysisMixin,
    CodingExpertProcessMixin,
):
    """Governed program synthesis, analysis, refactoring and upgrade authoring."""


__all__ = ["StarCodingExpert"]
