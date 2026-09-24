"""ai-assistant consolidated test suite (A57/E43) — rebuilt system.

Self-health collection for the 星澄 AI 投資管理與自動操盤系統 business
layer: service surface, manifest test targets, AI-connection contract and
the investment-mobile governed bridge.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
for _p in (
    str(_ROOT),
    str(_ROOT / "shared-layer" / "src"),
    str(_ROOT / "main-system" / "src-core"),
    str(_ROOT / "Standalone tools" / "ai-assistant" / "src"),
    str(_ROOT / "Standalone tools" / "ai-assistant" / "src" / "backend" / "services"),
):
    if _p not in sys.path:
        sys.path.insert(0, _p)

del _p

import pytest

from ai_nexus.application.service import AiNexusService
from ai_nexus.domain.contract import DOMAIN_IDS, TRADING_APP_VERSION

TOOL_ROOT = Path(__file__).resolve().parents[1]


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _service(tmp_path: Path) -> AiNexusService:
    return AiNexusService(tmp_path)


def test_manifest_test_targets_exist() -> None:
    manifest = json.loads(
        (TOOL_ROOT / "manifest.json").read_text(encoding="utf-8")
    )
    for target in manifest["test_targets"]:
        assert (TOOL_ROOT / target).is_file(), target


def test_service_surface_covers_six_domains(tmp_path: Path) -> None:
    service = _service(tmp_path)
    assert service.VERSION == TRADING_APP_VERSION
    for prefix in (
        "investment_tw_",
        "investment_us_",
        "investment_fund_",
        "investment_ai_",
        "investment_trade_",
        "investment_assets_",
    ):
        assert any(c.startswith(prefix) for c in service.COMMANDS), prefix
    # governed bridge envelopes preserved
    assert "investment_mobile_get_snapshot" in service.COMMANDS
    assert "investment_mobile_submit_instruction" in service.COMMANDS
    assert not service.owns("investment_watch_legacy_command")


def test_status_and_domains_bookkeeping(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _run(service.start())
    _, result = _run(service.handle("investment_status", {}))
    assert result["ok"] is True
    assert result["domains"] == list(DOMAIN_IDS)
    _, result = _run(service.handle("investment_domains", {}))
    assert len(result["domains"]) == 6
    _run(service.shutdown())


def test_unknown_command_fails_closed(tmp_path: Path) -> None:
    service = _service(tmp_path)
    with pytest.raises(PermissionError):
        _run(service.handle("bogus_command", {}))


def test_bridge_envelopes_round_trip(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _run(service.start())
    _, result = _run(
        service.handle(
            "investment_mobile_submit_instruction",
            {
                "operation": "record_signal",
                "signal": {
                    "signal_id": "sig-t1",
                    "market": "us",
                    "instrument": "AAPL",
                    "side": "buy",
                    "confidence": 0.8,
                    "quantity": 10,
                    "price": 200.0,
                },
            },
        )
    )
    assert result["ok"] is True
    _, result = _run(
        service.handle(
            "investment_mobile_submit_instruction",
            {"operation": "record_mode", "mode": "PAPER"},
        )
    )
    assert result["ok"] is True
    _, snapshot = _run(service.handle("investment_mobile_get_snapshot", {}))
    assert snapshot["ok"] is True
    assert snapshot["trading_mode"] == "PAPER"
    assert snapshot["signal_count"] >= 1
    _run(service.shutdown())


def test_bridge_rejects_unknown_operation(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _run(service.start())
    _, result = _run(
        service.handle(
            "investment_mobile_submit_instruction",
            {"operation": "drop_everything"},
        )
    )
    assert result["ok"] is False
    assert result["error_code"] == "OPERATION_UNKNOWN"
    _run(service.shutdown())
