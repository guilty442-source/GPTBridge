"""Dialogue generation controls: defaults must not masquerade as user requests."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SERVICES = ROOT / "model-dialogue" / "src" / "backend" / "services"
if str(SERVICES) not in sys.path:
    sys.path.insert(0, str(SERVICES))

from star_chat.application.service import StarChatService  # noqa: E402


def _infer_payload(payload: dict) -> dict:
    service = StarChatService()
    controls = service._resolve_generation_controls(payload)
    return service._infer_payload(
        payload,
        "prompt",
        "message",
        "",
        controls,
    )


def test_default_payload_does_not_override_automatic_assessment() -> None:
    result = _infer_payload({})

    assert result["task_intensity_mode"] == "automatic"
    assert "task_intensity" not in result
    assert "requested_task_intensity" not in result
    assert "reasoning_effort" not in result
    assert "reasoning_level" not in result
    assert "generation_speed" not in result
    assert result["max_output_tokens"] > 0


def test_explicit_controls_are_forwarded() -> None:
    result = _infer_payload(
        {
            "task_intensity": "difficult",
            "reasoning_level": "high-high",
            "generation_speed": "slow",
        }
    )

    assert result["task_intensity"] == "difficult"
    assert result["requested_task_intensity"] == "difficult"
    assert result["reasoning_effort"] == "high"
    assert result["reasoning_level"] == "high-high"
    assert result["generation_speed"] == "slow"


def test_explicit_reasoning_effort_is_forwarded_alone() -> None:
    result = _infer_payload({"reasoning_effort": "low"})

    assert result["reasoning_effort"] == "low"
    assert "task_intensity" not in result
