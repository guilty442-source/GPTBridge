"""Generation Router — strictly separated from Architecture Router.

    Architecture Router      Generation Router
    -------------------      -------------------
    hybrid-rag               general
    code-rag                 fast
    memory-rag               code
    agentic-rag              deep
                           visual

Any architecture may pair with any generation mode —
``code-rag + fast`` or ``hybrid-rag + deep`` are both legal.
Choosing the code retriever never silently selects qwen3-coder;
generation_mode = "code" is only the generation model choice.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class GenerationMode(str, Enum):
    GENERAL = "general"
    FAST = "fast"
    CODE = "code"
    DEEP = "deep"
    VISUAL = "visual"


# mode -> model registry key (resolution left to the model registry;
# this router only produces the routing decision).
MODE_MODEL_HINT: dict[GenerationMode, str] = {
    GenerationMode.GENERAL: "default-chat",
    GenerationMode.FAST: "small-fast",
    GenerationMode.CODE: "qwen3-coder",
    GenerationMode.DEEP: "deep-reasoning",
    GenerationMode.VISUAL: "vision",
}


@dataclass(frozen=True, slots=True)
class GenerationDecision:
    mode: GenerationMode
    model_hint: str
    reason: str


def route_generation(
    mode: str | GenerationMode = GenerationMode.GENERAL,
) -> GenerationDecision:
    """Resolve generation mode -> model hint.  Independent of which
    retrieval architectures produced the evidence."""
    try:
        gm = mode if isinstance(mode, GenerationMode) else GenerationMode(str(mode))
    except ValueError:
        gm = GenerationMode.GENERAL
        return GenerationDecision(gm, MODE_MODEL_HINT[gm], f"unknown-mode:{mode}->general")
    return GenerationDecision(gm, MODE_MODEL_HINT[gm], "explicit")


__all__ = [
    "GenerationDecision",
    "GenerationMode",
    "MODE_MODEL_HINT",
    "route_generation",
]
