from __future__ import annotations

import hashlib
import json
import os
import re
import stat as stat_module
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from websockets.sync.client import connect


class ExternalBrowserResearch:
    """Optional research through the independent browser-session AI tool.

    This client never calls a model API and never queues work.  It connects only
    to an already-running, authenticated loopback WebSocket owned by the
    independent AI collaboration tool.
    """

    def __init__(self, environment_key: str = "GPTBRIDGE_EXTERNAL_AI_WS_URL") -> None:
        self.environment_key = environment_key

    @staticmethod
    def _valid_url(value: str) -> bool:
        parsed = urlsplit(value)
        query = parse_qs(parsed.query)
        token = str((query.get("token") or [""])[0])
        instance = str((query.get("instance") or [""])[0])
        return (
            parsed.scheme == "ws"
            and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
            and len(token) >= 32
            and len(instance) >= 16
        )

    def _discover_url(self) -> str:
        state_base = Path(
            str(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        )
        peer_id = "ai-collaboration"
        peer_root = (state_base / "GPTBridge" / "standalone" / peer_id).resolve()
        ipc_root = peer_root / "runtime" / "ipc"
        owner_path = ipc_root / f"standalone-{peer_id}-backend.json"
        token_path = ipc_root / "session-token"
        try:
            for candidate in (peer_root, ipc_root, owner_path, token_path):
                metadata = candidate.lstat()
                attributes = int(getattr(metadata, "st_file_attributes", 0) or 0)
                if stat_module.S_ISLNK(metadata.st_mode) or bool(attributes & 0x400):
                    return ""
            owner = json.loads(owner_path.read_text(encoding="utf-8"))
            token = token_path.read_text(encoding="utf-8").strip().lower()
            port = int(owner.get("backend_port") or 0)
            expected_instance = hashlib.sha256(
                os.path.normcase(str(peer_root)).replace("\\", "/").encode("utf-8")
            ).hexdigest()[:24]
            if (
                str(owner.get("tool_id") or "") != peer_id
                or Path(str(owner.get("project_root") or "")).resolve() != peer_root
                or str(owner.get("workspace_instance_id") or "") != expected_instance
                or not 1024 <= port <= 65535
                or not re.fullmatch(r"[a-f0-9]{64}", token)
            ):
                return ""
            return (
                f"ws://127.0.0.1:{port}/?token={token}"
                f"&instance={expected_instance}"
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return ""

    def resolved_url(self) -> str:
        discovered = self._discover_url()
        if self._valid_url(discovered):
            return discovered
        configured = str(os.environ.get(self.environment_key) or "").strip()
        if self._valid_url(configured):
            return configured
        return ""

    def configured(self) -> bool:
        return bool(self.resolved_url())

    def search(self, unresolved: list[dict[str, str]]) -> dict[str, Any]:
        if not unresolved:
            return {"ok": True, "queued": False, "requested_count": 0}
        url = self.resolved_url()
        if not url:
            return {
                "ok": False,
                "queued": False,
                "transport": "browser-session-via-authenticated-websocket",
                "uses_api": False,
                "error_code": "EXTERNAL_AI_NOT_CONNECTED",
                "message": "外部 AI 瀏覽器協作工具尚未連線；搜尋未送出且不排隊。",
            }
        prompt = (
            "你是星澄的外部網路搜尋協作者。請使用目前已登入的瀏覽器能力，"
            "針對下列無法由官方來源解析的投資標的尋找官方代碼、最近價格或淨值、"
            "觀測日期、幣別、配息公告與來源網址。不得猜測數值；每個數值都要附來源。"
            "只回報研究結果，不提供買賣建議。\n\n"
            + json.dumps(unresolved, ensure_ascii=False)
        )
        try:
            with connect(
                url,
                origin="http://127.0.0.1:5180",
                open_timeout=5,
                close_timeout=2,
                max_size=16 * 1024 * 1024,
            ) as socket:
                socket.send(
                    json.dumps(
                        {"command": "ai_nexus_send_message", "payload": {"content": prompt}},
                        ensure_ascii=False,
                    )
                )
                while True:
                    raw = socket.recv(timeout=180)
                    message = json.loads(str(raw))
                    if message.get("event") != "ai_nexus_send_message_result":
                        continue
                    result = message.get("payload")
                    if not isinstance(result, dict):
                        result = {"ok": True, "result": result}
                    if result.get("ok") is not True:
                        return {
                            "ok": False,
                            "queued": False,
                            "transport": "browser-session-via-authenticated-websocket",
                            "uses_api": False,
                            "message": str(result.get("message") or "外部 AI 搜尋失敗。"),
                        }
                    group = result.get("group_message")
                    responses = (
                        group.get("responses", []) if isinstance(group, dict) else []
                    )
                    return {
                        "ok": True,
                        "queued": False,
                        "transport": "browser-session-via-authenticated-websocket",
                        "uses_api": False,
                        "responses": [
                            {
                                "agent_id": str(item.get("agent_id") or ""),
                                "status": str(item.get("status") or ""),
                                "content": str(item.get("content") or ""),
                                "error": str(item.get("error") or ""),
                            }
                            for item in responses
                            if isinstance(item, dict)
                        ],
                    }
        except Exception as error:
            return {
                "ok": False,
                "queued": False,
                "transport": "browser-session-via-authenticated-websocket",
                "uses_api": False,
                "error_code": "EXTERNAL_AI_CONNECTION_FAILED",
                "message": f"外部 AI 瀏覽器協作連線失敗：{type(error).__name__}",
            }
