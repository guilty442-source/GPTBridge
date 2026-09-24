# -*- coding: utf-8 -*-
"""P21：converse 受管工具迴圈接入生產 infer 路徑的接線測試。"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(
    0,
    str(
        Path(__file__).resolve().parents[1]
        / "src"
        / "backend"
        / "services"
        / "xingcheng"
        / "infrastructure"
    ),
)

import _xingcheng_test_support  # noqa: F401,E402

from native_transformer.inference.chat_session import SessionReply  # noqa: E402
from xingcheng.application.infer_planning import InferPlanningMixin  # noqa: E402


class _StubSession:
    def __init__(self, replies):
        self.replies = list(replies)
        self.tool_results = []

    def step(self, user_content=None, **_kwargs):
        return self.replies.pop(0)

    def add_tool_result(self, content, *, name=None):
        self.tool_results.append((name, content))


def _reply(text, calls=()):
    return SessionReply(
        text=text,
        raw_text=text,
        tool_calls=tuple(calls),
        generated_tokens=4,
        stopped_by_eos=True,
    )


class _FakeEngine:
    def __init__(self, session):
        self._session = session
        self.device = SimpleNamespace(type="cpu")
        self.config = SimpleNamespace(max_position_embeddings=512)
        self.checkpoint_path = "ckpt.pt"
        self.state_sha256 = "deadbeef"
        self.seen_system_prompt = None

    def new_chat_session(self, *, system_prompt=None, max_context=None):
        self.seen_system_prompt = system_prompt
        return self._session


class _Svc(InferPlanningMixin):
    """承接 mixin 的最小宿主：提供 handle/_record_latency。"""

    def __init__(self):
        self.dispatched = []
        self.native_runtime = SimpleNamespace(enabled=True)
        self._latencies = []

    async def handle(self, command, payload):
        self.dispatched.append((command, dict(payload)))
        return f"{command}_result", {"ok": True, "echo": "pong"}

    def _record_latency(self, stage, started):
        self._latencies.append(stage)


def _patch_engine(monkeypatch, engine):
    import xingcheng.infrastructure.native_engine as ne

    monkeypatch.setattr(ne, "native_engine_for", lambda *a, **k: engine)
    monkeypatch.setattr(
        ne, "generation_defaults", lambda: {"max_new_tokens": 16}
    )
    monkeypatch.setattr(ne, "cpu_generation_cap", lambda: 16)


def _patch_cpp_mode(monkeypatch, mode):
    import xingcheng.infrastructure.native_transformer.cpp_runtime as cr

    monkeypatch.setattr(cr, "cpp_runtime_mode", lambda: mode)


@pytest.mark.asyncio
async def test_converse_tools_executes_calls_and_surfaces_proposals(
    monkeypatch,
):
    session = _StubSession(
        [
            _reply("", calls=[{"name": "status", "arguments": {}}]),
            _reply(
                "",
                calls=[
                    {
                        "name": "propose_system_modification",
                        "arguments": {
                            "summary": "調整輪詢間隔",
                            "operation": "config_value",
                            "scope": "runtime",
                            "target": "interval",
                            "risk": "low",
                            "rollback": "restore",
                        },
                    }
                ],
            ),
            _reply("狀態良好，提案已排隊"),
        ]
    )
    engine = _FakeEngine(session)
    _patch_cpp_mode(monkeypatch, "fallback")
    _patch_engine(monkeypatch, engine)

    svc = _Svc()
    out = await svc._infer_converse_tools(
        {"tools_enabled": True}, "平台狀態如何", "conversation"
    )

    assert out is not None and out["ok"] is True
    assert out["tools_enabled"] is True
    assert out["response"] == "狀態良好，提案已排隊"
    assert out["tool_rounds"] == 2
    assert [t["name"] for t in out["tool_trace"]] == [
        "status",
        "propose_system_modification",
    ]
    # 唯讀命令確實走受管 dispatch
    assert svc.dispatched == [("xingcheng_status", {})]
    # 提案僅浮上、未執行
    proposals = out["system_modification_proposals"]
    assert len(proposals) == 1
    assert proposals[0]["summary"] == "調整輪詢間隔"
    assert proposals[0]["detail"]["operation"] == "config_value"
    # 預設 system prompt 宣告工具白名單
    assert "tool_call" in (engine.seen_system_prompt or "")


@pytest.mark.asyncio
async def test_converse_tools_skipped_when_cpp_required(monkeypatch):
    _patch_cpp_mode(monkeypatch, "required")
    svc = _Svc()
    out = await svc._infer_converse_tools(
        {"tools_enabled": True}, "hi", "conversation"
    )
    assert out is None


@pytest.mark.asyncio
async def test_converse_tools_skipped_when_engine_unavailable(monkeypatch):
    _patch_cpp_mode(monkeypatch, "off")
    import xingcheng.infrastructure.native_engine as ne

    def _boom(*a, **k):
        raise FileNotFoundError("no checkpoint")

    monkeypatch.setattr(ne, "native_engine_for", _boom)
    svc = _Svc()
    out = await svc._infer_converse_tools(
        {"tools_enabled": True}, "hi", "conversation"
    )
    assert out is None


@pytest.mark.asyncio
async def test_generate_model_output_routes_to_converse(monkeypatch):
    """tools_enabled＋原生路徑 → _infer_generate_model_output 走迴圈分支。"""
    session = _StubSession([_reply("完成")])
    engine = _FakeEngine(session)
    _patch_cpp_mode(monkeypatch, "off")
    _patch_engine(monkeypatch, engine)

    svc = _Svc()
    error, output = await svc._infer_generate_model_output(
        {"tools_enabled": True},
        "hello",
        "conversation",
        planned_profile=None,
        native_model_requested=True,
    )
    assert error is None
    assert output["tools_enabled"] is True
    assert output["response"] == "完成"
    assert svc._latencies == ["inference"]


@pytest.mark.asyncio
async def test_generate_model_output_converse_skipped_for_non_native(
    monkeypatch,
):
    """非原生路徑（transformer runtime 啟用＋未指定原生模型）不走迴圈。"""
    svc = _Svc()

    def _runtime(*a, **k):
        return {"ok": True, "text": "native"}

    svc._prepare_runtime_output = _runtime  # type: ignore[attr-defined]
    error, output = await svc._infer_generate_model_output(
        {"tools_enabled": True},
        "hello",
        "conversation",
        planned_profile=None,
        native_model_requested=False,
    )
    assert error is None
    assert output["text"] == "native"
    assert "tools_enabled" not in output
