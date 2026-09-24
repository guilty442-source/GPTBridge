"""Star-owned persistence, local inference, and market-data adapters."""

from .fast_inference import FastInferenceEngine, create_fast_engine

__all__ = [
    "FastInferenceEngine",
    "create_fast_engine",
]
