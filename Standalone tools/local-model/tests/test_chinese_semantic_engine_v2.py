"""chinese-semantic-engine v2 — 星澄內部模組測試。

v2 引擎已收編為 xingcheng runtime 的內部模組
（``xingcheng.infrastructure.chinese_semantic_engine_v2``），不再是獨立工具。
測試涵蓋：套件 import 面、引擎建構、設定預設值、hook 驗證、
health_check 結構、錯誤型別——不依賴外部服務（無 DB／Qdrant／模型）。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SERVICES = ROOT / "Standalone tools" / "local-model" / "src" / "backend" / "services"
SHARED_SRC = ROOT / "shared-layer" / "src"
for _p in (str(SERVICES), str(SHARED_SRC), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)
del _p


def test_package_import_surface() -> None:
    from xingcheng.infrastructure import chinese_semantic_engine_v2 as mod

    for name in (
        "ChineseSemanticEngineV2", "XingchengSemanticProcessor",
        "SemanticConfig", "SemanticRequest",
        "SemanticResponse", "SemanticProcessingError",
        "SemanticModelUnavailable", "SemanticValidationError",
        "create_engine", "create_initialized_engine",
    ):
        assert name in mod.__all__, name


def test_module_identity_is_internal() -> None:
    """v2 是 xingcheng 的內部模組，不再是 standalone-service。"""
    from xingcheng.infrastructure import chinese_semantic_engine_v2 as mod

    assert mod.COMPONENT_ID == "chinese-semantic-engine"
    assert mod.OWNER_SOVEREIGN == "xingcheng-domain"
    assert mod.RUNTIME_FORM == "internal-module"


def test_engine_construction_and_defaults() -> None:
    from xingcheng.infrastructure.chinese_semantic_engine_v2 import (
        SemanticConfig,
        create_engine,
    )

    engine = create_engine()
    cfg = engine.config
    assert isinstance(cfg, SemanticConfig)
    assert cfg.model_endpoint == "xingcheng"
    assert cfg.max_context_tokens > 0
    assert cfg.timeout_seconds > 0
    assert engine.processor is None  # processor 需另行注入


def test_request_response_dataclass_contract() -> None:
    from xingcheng.infrastructure.chinese_semantic_engine_v2 import (
        SemanticRequest,
        SemanticResponse,
    )

    req = SemanticRequest(text="測試文本", operation="analyze")
    assert req.priority_class == "interactive"
    assert req.parameters == {}
    resp = SemanticResponse(result={"ok": True})
    assert resp.degraded is False
    assert resp.warnings == ()


def test_hook_registration_validation() -> None:
    from xingcheng.infrastructure.chinese_semantic_engine_v2 import create_engine

    engine = create_engine()
    with pytest.raises(ValueError):
        engine.register_hook("not_a_hook", lambda r: r)

    async def _noop(request):
        return request

    engine.register_hook("pre_analyze", _noop)


def test_unknown_operation_fail_closed() -> None:
    from xingcheng.infrastructure.chinese_semantic_engine_v2 import (
        SemanticValidationError,
        create_engine,
    )

    engine = create_engine()
    with pytest.raises(SemanticValidationError):
        asyncio.run(engine.process("文本", operation="bogus"))


def test_missing_processor_fail_closed() -> None:
    """未注入 processor 時 analyze 必須 fail-closed，不允許靜默降級。"""
    from xingcheng.infrastructure.chinese_semantic_engine_v2 import (
        SemanticModelUnavailable,
        create_engine,
    )

    engine = create_engine()
    with pytest.raises(SemanticModelUnavailable):
        asyncio.run(engine.process("文本", operation="analyze"))


def test_health_check_structure() -> None:
    from xingcheng.infrastructure.chinese_semantic_engine_v2 import create_engine

    engine = create_engine()
    health = asyncio.run(engine.health_check())
    assert health["component"] == "chinese-semantic-engine"
    assert health["version"]
    assert "initialized" in health
    assert "config" in health
    # 未注入 processor 時 engine_initialized 為 False（誠實狀態）
    assert health["initialized"] is False


def test_analyze_roundtrip_with_native_processor() -> None:
    """注入 XingchengSemanticProcessor 後 analyze 走星澄 v1 引擎。"""
    from xingcheng.infrastructure.chinese_semantic_engine_v2 import (
        SemanticRequest,
        XingchengSemanticProcessor,
        SemanticConfig,
        create_engine,
    )

    engine = create_engine(processor=XingchengSemanticProcessor(SemanticConfig()))
    asyncio.run(engine.initialize())
    resp = asyncio.run(
        engine.analyze(SemanticRequest(text="請幫我查詢明天天氣", operation="analyze"))
    )
    assert resp.result  # star-semantic-plan/v1 投影
    assert resp.metadata["model"] == "xingcheng-native"
