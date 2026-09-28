from __future__ import annotations

import sys
from pathlib import Path


TOOL_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = Path(__file__).resolve().parents[3]
SERVICES_ROOT = TOOL_ROOT / "src" / "backend" / "services"
SRC_ROOT = TOOL_ROOT / "src"
SHARED_SRC = WORKSPACE_ROOT / "shared-layer" / "src"

for path in (SERVICES_ROOT, SRC_ROOT, SHARED_SRC):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import pytest


@pytest.fixture(autouse=True)
def offline_ollama_probe(monkeypatch: pytest.MonkeyPatch):
    """A57: tests never spawn the real ``ollama`` CLI — the probe hangs when
    the binary exists but its daemon is down. Pin it to not-ready."""
    from ai_nexus.integration.star_channel import InvestmentAiConnections

    monkeypatch.setattr(
        InvestmentAiConnections, "_ollama_ready", staticmethod(lambda: False)
    )
