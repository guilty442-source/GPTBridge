"""Synthetic fixture generator for visual smoke tests."""

from __future__ import annotations

from typing import Any

from smoke_fixture_data import (
    fixture_diagnostics,
    fixture_mobile_sync,
    fixture_state,
)


def synthetic_fixture() -> dict[str, Any]:
    generated_at = "2026-07-28T15:30:00+00:00"
    state = fixture_state(generated_at)
    return {
        "state_revision": "visual-fixture-v1",
        "state": state,
        "diagnostics": fixture_diagnostics(generated_at),
        "mobile_sync": fixture_mobile_sync(),
        "market_sessions": state["market_sessions"],
    }
