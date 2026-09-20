"""Governed lifecycle control for Xingcheng's native first-party model.

This module manages the first-party StarAutoregressiveLanguageModel that runs
without any third-party weights or Ollama dependency.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .xingcheng_native_model_constants import _MAIN_MODEL_ID

_LOCAL_MODEL_ROOT = Path(__file__).resolve().parents[3] / "Standalone tools" / "local-model"


def _native_model_status() -> dict[str, Any]:
    """Return the native first-party model status."""
    checked_at = datetime.now(timezone.utc).isoformat()

    try:
        sys.path.insert(0, str(_LOCAL_MODEL_ROOT / "src" / "backend"))
        from services.xingcheng.infrastructure.generative_language_model import (
            StarAutoregressiveLanguageModel,
        )
        from services.xingcheng.infrastructure.native_model import (
            StarNativeLanguageModel,
        )

        model = StarNativeLanguageModel(model_role="main")
        metrics = model.training_status()

        return {
            "model_id": _MAIN_MODEL_ID,
            "state": "ready",
            "running": True,
            "available": True,
            "checked_at": checked_at,
            "message": "",
            "model_type": metrics.get("model_type", "weighted-backoff-token-ngram"),
            "vocabulary_size": metrics.get("vocabulary_size", 0),
            "base_example_count": metrics.get("base_example_count", 0),
            "learned_example_count": metrics.get("learned_example_count", 0),
            "third_party_weights_used": False,
        }
    except Exception as exc:
        return {
            "model_id": _MAIN_MODEL_ID,
            "state": "unavailable",
            "running": False,
            "available": False,
            "checked_at": checked_at,
            "message": f"{type(exc).__name__}: {exc}",
            "third_party_weights_used": False,
        }


def _start_native_model() -> None:
    """Initialize the native model (no-op for first-party model, always ready)."""
    sys.path.insert(0, str(_LOCAL_MODEL_ROOT / "src" / "backend"))
    from services.xingcheng.infrastructure.native_model import StarNativeLanguageModel

    _ = StarNativeLanguageModel(model_role="main")


def _stop_native_model() -> None:
    """Stop the native model (no-op for first-party model)."""
    pass


async def set_native_model_enabled(enabled: bool) -> dict[str, Any]:
    """Apply a user-requested lifecycle transition and verify the result."""
    action = _start_native_model if enabled else _stop_native_model
    try:
        await asyncio.to_thread(action)
    except (OSError, RuntimeError, Exception) as exc:
        status = await asyncio.to_thread(_native_model_status)
        return {
            "ok": False,
            "error_code": "NATIVE_MODEL_TRANSITION_FAILED",
            "message": str(exc),
            **status,
        }
    status = await asyncio.to_thread(_native_model_status)
    expected = status["running"] is enabled
    return {
        "ok": expected,
        "error_code": "" if expected else "NATIVE_MODEL_STATE_MISMATCH",
        "message": "" if expected else "native model state verification failed",
        **status,
    }


async def native_model_infer(
    prompt: str,
    *,
    intent: str = "conversation",
    grounding: str = "",
    max_tokens: int = 180,
    temperature: float = 0.55,
    top_k: int = 4,
) -> dict[str, Any]:
    """Run inference using the first-party native language model."""
    try:
        sys.path.insert(0, str(_LOCAL_MODEL_ROOT / "src" / "backend"))
        from services.xingcheng.infrastructure.native_model import (
            StarNativeLanguageModel,
        )

        model = StarNativeLanguageModel(model_role="main")
        result = model.language_model.generate(
            intent=intent,
            prompt=prompt,
            grounding=grounding,
            max_tokens=max_tokens,
            temperature=temperature,
            top_k=top_k,
        )
        result["model_id"] = _MAIN_MODEL_ID
        result["third_party_weights_used"] = False
        return {"ok": True, **result}
    except Exception as exc:
        return {
            "ok": False,
            "error_code": "NATIVE_MODEL_INFERENCE_FAILED",
            "message": str(exc),
            "model_id": _MAIN_MODEL_ID,
            "third_party_weights_used": False,
        }


def native_model_status() -> dict[str, Any]:
    """Return live model state."""
    return _native_model_status()


__all__ = ["native_model_status", "set_native_model_enabled", "native_model_infer"]