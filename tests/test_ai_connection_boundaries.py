from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC_CORE = ROOT / "src-core"
if str(SRC_CORE) not in sys.path:
    sys.path.insert(0, str(SRC_CORE))

from tasks.toolbox_service import ToolboxService


def _load_ai_connections_module():
    path = (
        ROOT
        / "platform_tools"
        / "ai-assistant"
        / "src"
        / "backend"
        / "services"
        / "ai_nexus"
        / "ai_connections.py"
    )
    spec = importlib.util.spec_from_file_location("test_ai_connections", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_ai_tools_are_independent_v1_applications() -> None:
    manifests = {
        path.parent.name: json.loads(path.read_text(encoding="utf-8"))
        for path in (ROOT / "platform_tools").glob("*/manifest.json")
    }
    assert manifests["local-ai"]["name"] == "星澄"
    assert manifests["local-ai"]["version"] == "1.0.0"
    assert manifests["ai-collaboration"]["name"] == "外部AI協作"
    assert manifests["ai-assistant"]["version"] == "1.0.0"
    connections = manifests["ai-assistant"]["capabilities"]["ai-connections"]
    assert connections["peers"] == ["local-ai", "ai-collaboration"]
    assert connections["queue_when_offline"] is False
    assert connections["share_database"] is False
    for tool_id in ("local-ai", "ai-collaboration", "ai-assistant"):
        assert manifests[tool_id]["permissions"]["database_scope"] == "tool-database-only"


def test_unconfigured_ai_peer_fails_without_queue(tmp_path: Path, monkeypatch) -> None:
    module = _load_ai_connections_module()
    monkeypatch.delenv("GPTBRIDGE_LOCAL_AI_WS_URL", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    peer = module.AuthenticatedAiPeer("星澄", "GPTBRIDGE_LOCAL_AI_WS_URL")
    result = asyncio.run(peer.request("local_ai_infer", {"prompt": "test"}))
    assert result["ok"] is False
    assert result["queued"] is False
    assert result["error_code"] == "AI_PEER_NOT_CONNECTED"


def test_ai_peer_rediscovers_current_loopback_session_after_restart(
    tmp_path: Path, monkeypatch
) -> None:
    module = _load_ai_connections_module()
    monkeypatch.delenv("GPTBRIDGE_LOCAL_AI_WS_URL", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    peer_id = "local-ai"
    peer_root = (tmp_path / "GPTBridge" / "standalone" / peer_id).resolve()
    ipc_root = peer_root / "runtime" / "ipc"
    ipc_root.mkdir(parents=True)
    token = "b" * 64
    normalized = os.path.normcase(str(peer_root)).replace("\\", "/")
    instance = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]
    (ipc_root / "session-token").write_text(token, encoding="utf-8")
    (ipc_root / f"standalone-{peer_id}-backend.json").write_text(
        json.dumps(
            {
                "tool_id": peer_id,
                "project_root": str(peer_root),
                "workspace_instance_id": instance,
                "backend_port": 24002,
            }
        ),
        encoding="utf-8",
    )

    peer = module.AuthenticatedAiPeer("local-ai", "GPTBRIDGE_LOCAL_AI_WS_URL")
    assert peer.resolved_url() == (
        f"ws://127.0.0.1:24002/?token={token}&instance={instance}"
    )


def test_external_discussion_returns_only_current_responses(monkeypatch) -> None:
    module = _load_ai_connections_module()
    connections = module.InvestmentAiConnections()
    monkeypatch.setattr(connections.external, "configured", lambda: True)
    monkeypatch.setattr(
        connections.external,
        "request_sync",
        lambda *_args, **_kwargs: {
            "ok": True,
            "group_message": {
                "responses": [
                    {
                        "agent_id": "browser-ai",
                        "status": "completed",
                        "content": "討論內容",
                        "error": "",
                    }
                ]
            },
            "messages": [{"content": "不應跨資料庫傳回的歷史訊息"}],
            "memory_items": [{"content": "不應傳回的外部記憶"}],
        },
    )

    result = connections.discuss_analysis_sync({"owner": "星澄"})

    assert result["ok"] is True
    assert result["uses_api"] is False
    assert result["responses"][0]["content"] == "討論內容"
    assert "messages" not in result
    assert "memory_items" not in result


def test_main_injects_only_manifest_declared_active_peer_session(
    tmp_path: Path, monkeypatch
) -> None:
    project_root = tmp_path / "project"
    local_app_data = tmp_path / "local-app-data"
    peer_id = "local-ai"
    peer_manifest = project_root / "platform_tools" / peer_id / "manifest.json"
    peer_manifest.parent.mkdir(parents=True)
    peer_manifest.write_text(
        json.dumps({"id": peer_id, "enabled": True}), encoding="utf-8"
    )
    assistant_root = project_root / "platform_tools" / "ai-assistant"
    assistant_root.mkdir(parents=True)
    peer_root = local_app_data / "GPTBridge" / "standalone" / peer_id
    ipc_root = peer_root / "runtime" / "ipc"
    ipc_root.mkdir(parents=True)
    token = "a" * 64
    normalized = os.path.normcase(str(peer_root.resolve())).replace("\\", "/")
    instance = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]
    (ipc_root / "session-token").write_text(token, encoding="utf-8")
    (ipc_root / f"standalone-{peer_id}-backend.json").write_text(
        json.dumps(
            {
                "tool_id": peer_id,
                "project_root": str(peer_root.resolve()),
                "workspace_instance_id": instance,
                "backend_port": 24001,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("LOCALAPPDATA", str(local_app_data))
    manifest = {
        "environment": {"allow": ["GPTBRIDGE_LOCAL_AI_WS_URL"]},
        "capabilities": {
            "ai-connections": {
                "peers": [peer_id],
                "bindings": {peer_id: "GPTBRIDGE_LOCAL_AI_WS_URL"},
            }
        },
    }
    environment = ToolboxService(project_root)._tool_environment(
        "ai-assistant", assistant_root, manifest
    )
    assert environment["GPTBRIDGE_LOCAL_AI_WS_URL"] == (
        f"ws://127.0.0.1:24001/?token={token}&instance={instance}"
    )
