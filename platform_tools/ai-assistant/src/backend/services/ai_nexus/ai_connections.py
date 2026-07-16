from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import stat as stat_module
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import websockets
from websockets.sync.client import connect as sync_connect


class AuthenticatedAiPeer:
    """One-shot authenticated peer call. Commands are never queued."""

    def __init__(self, peer_id: str, environment_key: str) -> None:
        self.peer_id = peer_id
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
        """Recover a current peer descriptor after peer restarts.

        The descriptor contains only a short-lived loopback address and token;
        no peer database or application data is opened.
        """

        state_base = Path(
            str(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        )
        peer_root = (state_base / "GPTBridge" / "standalone" / self.peer_id).resolve()
        ipc_root = peer_root / "runtime" / "ipc"
        owner_path = ipc_root / f"standalone-{self.peer_id}-backend.json"
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
                str(owner.get("tool_id") or "") != self.peer_id
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

    async def request(
        self,
        command: str,
        payload: dict[str, Any],
        *,
        timeout_seconds: float = 45,
    ) -> dict[str, Any]:
        url = self.resolved_url()
        if not url:
            return {
                "ok": False,
                "peer": self.peer_id,
                "queued": False,
                "error_code": "AI_PEER_NOT_CONNECTED",
                "message": f"{self.peer_id} authenticated connection is not ready",
            }
        try:
            async with websockets.connect(
                url,
                origin="http://127.0.0.1:5180",
                open_timeout=5,
                close_timeout=2,
            ) as socket:
                await socket.send(json.dumps({"command": command, "payload": payload}))
                while True:
                    raw = await asyncio.wait_for(socket.recv(), timeout=timeout_seconds)
                    message = json.loads(str(raw))
                    if message.get("event") != f"{command}_result":
                        continue
                    result = message.get("payload")
                    if isinstance(result, dict):
                        return {**result, "peer": self.peer_id, "queued": False}
                    return {"ok": True, "peer": self.peer_id, "queued": False, "result": result}
        except Exception as error:
            return {
                "ok": False,
                "peer": self.peer_id,
                "queued": False,
                "error_code": "AI_PEER_CONNECTION_FAILED",
                "message": f"{self.peer_id} connection failed: {type(error).__name__}",
            }

    def request_sync(
        self,
        command: str,
        payload: dict[str, Any],
        *,
        timeout_seconds: float = 90,
    ) -> dict[str, Any]:
        """Synchronous variant for investment workers running in a thread."""

        url = self.resolved_url()
        if not url:
            return {
                "ok": False,
                "peer": self.peer_id,
                "queued": False,
                "error_code": "AI_PEER_NOT_CONNECTED",
                "message": f"{self.peer_id} authenticated connection is not ready",
            }
        try:
            with sync_connect(
                url,
                origin="http://127.0.0.1:5180",
                open_timeout=5,
                close_timeout=2,
                max_size=16 * 1024 * 1024,
            ) as socket:
                socket.send(json.dumps({"command": command, "payload": payload}))
                while True:
                    raw = socket.recv(timeout=timeout_seconds)
                    message = json.loads(str(raw))
                    if message.get("event") != f"{command}_result":
                        continue
                    result = message.get("payload")
                    if isinstance(result, dict):
                        return {**result, "peer": self.peer_id, "queued": False}
                    return {
                        "ok": True,
                        "peer": self.peer_id,
                        "queued": False,
                        "result": result,
                    }
        except Exception as error:
            return {
                "ok": False,
                "peer": self.peer_id,
                "queued": False,
                "error_code": "AI_PEER_CONNECTION_FAILED",
                "message": f"{self.peer_id} connection failed: {type(error).__name__}",
            }


class InvestmentAiConnections:
    def __init__(self) -> None:
        self.local = AuthenticatedAiPeer("星澄", "GPTBRIDGE_LOCAL_AI_WS_URL")
        self.external = AuthenticatedAiPeer(
            "外部AI協作", "GPTBRIDGE_EXTERNAL_AI_WS_URL"
        )

    def status(self) -> dict[str, Any]:
        return {
            "ok": True,
            "transport": "authenticated-websocket",
            "external_ai_transport": "browser-session-via-independent-tool",
            "external_ai_uses_api": False,
            "queue_when_offline": False,
            "database_shared": False,
            "roles": {
                "investment_manager": "portfolio-state-and-settings",
                "local_ai": "market-search-and-investment-analysis",
                "external_ai": "discussion-only",
            },
            "peers": {
                "local_ai": {"name": "星澄", "configured": self.local.configured()},
                "external_ai": {
                    "name": "外部AI協作",
                    "configured": self.external.configured(),
                },
            },
        }

    async def consult(self, prompt: str, scope: str = "both") -> dict[str, Any]:
        if not prompt.strip():
            return {"ok": False, "message": "prompt is required"}
        calls = []
        names = []
        if scope in {"both", "local"}:
            names.append("local_ai")
            calls.append(self.local.request("local_ai_infer", {"prompt": prompt}))
        if scope in {"both", "external"}:
            names.append("external_ai")
            calls.append(
                self.external.request("ai_nexus_send_message", {"content": prompt})
            )
        if not calls:
            return {"ok": False, "message": "scope must be local, external, or both"}
        values = await asyncio.gather(*calls)
        results = dict(zip(names, values))
        return {
            "ok": all(value.get("ok") is True for value in values),
            "queued": False,
            "results": results,
        }

    def search_investments_sync(
        self,
        holdings: list[dict[str, Any]],
        *,
        allow_external_fallback: bool = False,
    ) -> dict[str, Any]:
        return self.local.request_sync(
            "local_ai_search_investments",
            {
                "holdings": [
                    {
                        key: item.get(key)
                        for key in (
                            "symbol",
                            "name",
                            "market",
                            "asset_type",
                            "currency",
                            "fund_quote_symbol",
                            "isin",
                        )
                    }
                    for item in holdings
                    if isinstance(item, dict)
                ],
                "require_all": False,
                "allow_external_fallback": allow_external_fallback,
            },
            timeout_seconds=180,
        )

    def analyze_investments_sync(
        self,
        holdings: list[dict[str, Any]],
        analysis_parameters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.local.request_sync(
            "local_ai_analyze_investments",
            {
                "holdings": holdings,
                "analysis_parameters": dict(analysis_parameters or {}),
            },
            timeout_seconds=120,
        )

    def discuss_analysis_sync(self, analysis: dict[str, Any]) -> dict[str, Any]:
        if not self.external.configured():
            return {
                "ok": False,
                "queued": False,
                "error_code": "EXTERNAL_AI_NOT_CONNECTED",
                "message": "外部 AI 協作工具尚未連線；分析結果未排隊。",
            }
        prompt = (
            "請針對以下由星澄產生、已含來源限制的投資分析進行討論。"
            "不得補造報價、配息或交易事實；若需新增事實，請附可驗證來源網址與日期。"
            "只提出風險、反例與待查證事項，不執行交易或變更設定。\n\n"
            + json.dumps(analysis, ensure_ascii=False)
        )
        raw = self.external.request_sync(
            "ai_nexus_send_message",
            {"content": prompt},
            timeout_seconds=180,
        )
        if raw.get("ok") is not True:
            return {
                "ok": False,
                "queued": False,
                "error_code": str(raw.get("error_code") or "EXTERNAL_AI_DISCUSSION_FAILED"),
                "message": str(raw.get("message") or "外部 AI 討論失敗。"),
                "transport": "browser-session-via-authenticated-websocket",
                "uses_api": False,
            }
        group = raw.get("group_message")
        responses = group.get("responses", []) if isinstance(group, dict) else []
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
