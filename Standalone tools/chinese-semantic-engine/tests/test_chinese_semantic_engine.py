"""chinese-semantic-engine self-health tests (G87 / W5-2).

稽核 self-health 要求本檔存在且可 collect／執行。測試涵蓋：
manifest 契約、套件 import 面、引擎建構、設定預設值、hook 驗證、
health_check 結構、錯誤型別——不依賴外部服務（無 DB／Qdrant／模型）。
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

TOOL_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = TOOL_ROOT.parents[1]
SERVICES_ROOT = TOOL_ROOT / "src" / "backend" / "services"
SHARED_SRC = PROJECT_ROOT / "shared-layer" / "src"
for _p in (str(SERVICES_ROOT), str(SHARED_SRC), str(PROJECT_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)
del _p


def test_manifest_contract() -> None:
    manifest = json.loads((TOOL_ROOT / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["id"] == "chinese-semantic-engine"
    assert manifest["version"]
    assert manifest["independent_tool"] is True
    assert manifest["lifecycle"]["startup"] == "on-demand"


def test_package_import_surface() -> None:
    import chinese_semantic_engine as pkg

    for name in (
        "ChineseSemanticEngineV2", "SemanticConfig", "SemanticRequest",
        "SemanticResponse", "SemanticProcessingError",
        "SemanticModelUnavailable", "SemanticValidationError",
        "create_engine", "ChannelRuntime",
    ):
        assert name in pkg.__all__, name


def test_engine_construction_and_defaults() -> None:
    from chinese_semantic_engine import SemanticConfig, create_engine

    engine = create_engine()
    cfg = engine.config
    assert isinstance(cfg, SemanticConfig)
    assert cfg.model_endpoint == "xingcheng"
    assert cfg.max_context_tokens > 0
    assert cfg.timeout_seconds > 0
    assert engine.processor is None  # processor 需另行注入


def test_request_response_dataclass_contract() -> None:
    from chinese_semantic_engine import SemanticRequest, SemanticResponse

    req = SemanticRequest(text="測試文本", operation="analyze")
    assert req.priority_class == "interactive"
    assert req.parameters == {}
    resp = SemanticResponse(result={"ok": True})
    assert resp.degraded is False
    assert resp.warnings == ()


def test_hook_registration_validation() -> None:
    from chinese_semantic_engine import create_engine

    engine = create_engine()
    with pytest.raises(ValueError):
        engine.register_hook("not_a_hook", lambda r: r)

    async def _noop(request):
        return request

    engine.register_hook("pre_analyze", _noop)


def test_unknown_operation_fail_closed() -> None:
    from chinese_semantic_engine import SemanticValidationError, create_engine

    engine = create_engine()
    with pytest.raises(SemanticValidationError):
        asyncio.run(engine.process("文本", operation="bogus"))


def test_health_check_structure() -> None:
    from chinese_semantic_engine import create_engine

    engine = create_engine()
    health = asyncio.run(engine.health_check())
    assert health["component"] == "chinese-semantic-engine"
    assert health["version"]
    assert "initialized" in health
    assert "config" in health
    # 未注入 processor 時 engine_initialized 為 False（誠實狀態）
    assert health["initialized"] is False
