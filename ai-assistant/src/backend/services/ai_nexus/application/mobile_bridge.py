from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from ..infrastructure.watch_repository import InvestmentWatchRepository


def local_device_now() -> datetime:
    return datetime.now().astimezone()


class InvestmentMobileBridgeMixin:
    async def _get_mobile_sync(self, _payload: dict[str, Any]) -> dict[str, Any]:
        response = self._state_response(self.repository.load_state())
        response["message"] = "手機版介面經治理通道共用 AI 投資管家的設定與投資資料。"
        response["network_policy"] = "served-by-independent-mobile-interface"
        return response

    async def _set_mobile_sync_enabled(self, payload: dict[str, Any]) -> dict[str, Any]:
        current = self._mobile_sync_status()
        enabled = payload.get("enabled") is True
        allow_lan = (
            payload.get("allow_lan") is True
            if "allow_lan" in payload
            else current["allow_lan"]
        )
        try:
            port = int(payload.get("port") or current["port"])
        except (TypeError, ValueError):
            port = int(current["port"])
        port = max(1024, min(port, 65535))
        self.analytics_store.set_setting("mobile_sync_enabled", enabled)
        self.analytics_store.set_setting("mobile_sync_allow_lan", allow_lan)
        self.analytics_store.set_setting("mobile_sync_port", port)
        response = self._state_response(self.repository.load_state())
        response.update(
            {
                "ok": True,
                "sync": self._mobile_sync_status(),
                "message": "手機版設定已存入 AI 投資管家的共用設定層。",
            }
        )
        return response

    async def _set_mobile_sync_remote_url(self, payload: dict[str, Any]) -> dict[str, Any]:
        del payload
        state = self.repository.save_mobile_sync_remote_url("")
        return {
            "ok": False,
            "error_code": "NETWORK_ACCESS_DISABLED",
            "message": "AI 投資管家禁止設定遠端橋接；服務由AI投資管家經 AI 通道提供。",
            "state": state,
            "diagnostics": self._diagnostics(state),
            "mobile_sync": self._mobile_sync_status(),
        }

    async def _rotate_mobile_sync_pairing(self, _payload: dict[str, Any]) -> dict[str, Any]:
        state = self.repository.load_state()
        return {
            "ok": False,
            "error_code": "NETWORK_ACCESS_DISABLED",
            "message": "AI 投資管家禁止手機同步。",
            "state": state,
            "diagnostics": self._diagnostics(state),
            "mobile_sync": self._mobile_sync_status(),
        }

    async def _revoke_mobile_sync_pairing(self, _payload: dict[str, Any]) -> dict[str, Any]:
        response = self._state_response(self.repository.load_state())
        response["message"] = "手機同步配對已撤銷；更新配對碼後才可再次連線。"
        return response

    def _mobile_sync_status(self, *, expose_pairing_code: bool = True) -> dict[str, Any]:
        del expose_pairing_code
        enabled = bool(self.analytics_store.get_setting("mobile_sync_enabled", False))
        allow_lan = bool(self.analytics_store.get_setting("mobile_sync_allow_lan", False))
        try:
            port = int(self.analytics_store.get_setting("mobile_sync_port", 18765))
        except (TypeError, ValueError):
            port = 18765
        return {
            "ok": True,
            "running": enabled,
            "enabled": enabled,
            "allow_lan": allow_lan,
            "port": port,
            "separated_tool_id": "investment-mobile",
            "main_system_independent_tool": True,
            "business_layer_owner": "ai-assistant",
            "settings_owner": "ai-assistant",
            "permission_profile": "ai-investment-manager-v1",
            "separate_business_layer": False,
            "separate_settings_layer": False,
            "network_policy": "served-by-independent-mobile-interface",
            "shared_scope_message": "僅在主系統視為獨立工具；業務與設定由 AI 投資管家共用。",
            "start_error": self._mobile_sync_start_error,
            "message": "僅在主系統視為獨立工具；設定與投資業務由 AI 投資管家共用。",
        }

    def _mobile_sync_snapshot(self) -> dict[str, Any]:
        state = self.repository.load_state()
        analytics = self.analytics_store.analytics_snapshot(state)
        diagnostics = self._diagnostics(state)
        return {
            "ok": True,
            "tool": "AI投資管家",
            "version": self.VERSION,
            "generated_at": local_device_now().isoformat(),
            "platform": {
                "schema": "gptbridge-investment-mobile/v1",
                "owner_tool": "investment-mobile",
                "source_tool": "ai-assistant",
            },
            "sync": self._mobile_sync_status(expose_pairing_code=False),
            "state": self._compact_mobile_state(state),
            "analytics": {
                "performance": analytics.get("performance"),
                "risk": analytics.get("risk"),
                "stress": analytics.get("stress"),
                "alerts": analytics.get("alerts"),
                "calibration": analytics.get("calibration"),
            },
            "diagnostics": diagnostics,
            "local_only": True,
        }

    async def _investment_mobile_get_snapshot(
        self, _payload: dict[str, Any]
    ) -> dict[str, Any]:
        snapshot = self._mobile_sync_snapshot()
        snapshot["connection_coordinator"] = "ai-assistant"
        snapshot["transport"] = "governance-authenticated-shared-layer"
        return snapshot

    async def _investment_mobile_submit_instruction(
        self, payload: dict[str, Any]
    ) -> dict[str, Any]:
        if str(payload.get("operation") or "") == "update_shared_settings":
            settings = payload.get("settings")
            if not isinstance(settings, dict):
                return {"ok": False, "error_code": "SETTINGS_REQUIRED"}
            return await self._set_mobile_sync_enabled(dict(settings))
        return await self._queue_mobile_xingcheng_command(
            str(payload.get("instruction") or "").strip()
        )

    def _schedule_mobile_xingcheng_command(self, instruction: str) -> dict[str, Any]:
        if not instruction.strip():
            return {"ok": False, "message": "請輸入AI投資管家命令"}
        if self._event_loop is None or self._event_loop.is_closed():
            return {"ok": False, "message": "手機工具尚未連到AI投資管家 AI 通道"}
        future = asyncio.run_coroutine_threadsafe(
            self._queue_mobile_xingcheng_command(instruction),
            self._event_loop,
        )
        return future.result(timeout=10)

    async def _queue_mobile_xingcheng_command(self, instruction: str) -> dict[str, Any]:
        state = self.repository.load_state()
        if not state.get("holdings"):
            return {
                "ok": False,
                "message": "請先在桌面端讀取持股檔案",
                "sync": self._mobile_sync_status(expose_pairing_code=False),
            }
        result = self._schedule_local_risk_ai_background(
            state,
            {
                "trigger": "mobile_remote_command",
                "instruction": instruction,
                "live_quotes": True,
            },
        )
        return {
            "ok": bool(result.get("ok")),
            "queued": bool(result.get("queued")),
            "message": result.get("message") or "AI投資管家命令已排入背景執行",
            "run": result.get("run"),
            "sync": self._mobile_sync_status(expose_pairing_code=False),
        }

    @staticmethod
    def _compact_mobile_state(state: dict[str, Any]) -> dict[str, Any]:
        portfolio = state.get("portfolio") if isinstance(state.get("portfolio"), dict) else None
        if isinstance(portfolio, dict):
            portfolio = {
                "file_name": portfolio.get("file_name") or "",
                "holding_count": portfolio.get("holding_count") or 0,
                "imported_at": portfolio.get("imported_at") or "",
            }

        holdings: list[dict[str, Any]] = []
        for holding in state.get("holdings", []):
            if not isinstance(holding, dict):
                continue
            holdings.append(
                {
                    "symbol": holding.get("symbol") or "",
                    "name": holding.get("name") or "",
                    "market": holding.get("market") or "",
                    "asset_type": holding.get("asset_type") or "",
                    "quantity": holding.get("quantity") or 0,
                    "average_cost": holding.get("average_cost"),
                    "currency": holding.get("currency") or "",
                }
            )

        runs: list[dict[str, Any]] = []
        for run in state.get("ai_runs", []):
            if not isinstance(run, dict):
                continue
            runs.append(
                {
                    "run_id": run.get("run_id") or "",
                    "role": run.get("role") or "",
                    "provider": run.get("provider") or "",
                    "status": run.get("status") or "",
                    "created_at": run.get("created_at") or "",
                    "finished_at": run.get("finished_at") or "",
                    "content": InvestmentWatchRepository._shorten(
                        str(run.get("content") or ""),
                        1200,
                    ),
                    "error": InvestmentWatchRepository._shorten(
                        str(run.get("error") or ""),
                        600,
                    ),
                }
            )
            if len(runs) >= 12:
                break

        command_result = (
            state.get("xingcheng_command_result")
            if isinstance(state.get("xingcheng_command_result"), dict)
            else None
        )
        return {
            "portfolio": portfolio,
            "holdings": holdings,
            "workbook_scan_quality": state.get("workbook_scan_quality"),
            "xingcheng_product_status": state.get("xingcheng_product_status"),
            "xingcheng_summary": state.get("xingcheng_summary"),
            "xingcheng_risk_warnings": list(state.get("xingcheng_risk_warnings", []))[:20],
            "xingcheng_command_result": command_result,
            "xingcheng_action_plan": list(state.get("xingcheng_action_plan", []))[:8],
            "xingcheng_watch_triggers": list(state.get("xingcheng_watch_triggers", []))[:12],
            "xingcheng_confidence": state.get("xingcheng_confidence"),
            "xingcheng_decision_brief": state.get("xingcheng_decision_brief") or "",
            "xingcheng_network_context": state.get("xingcheng_network_context"),
            "shared_memory": InvestmentWatchRepository._shorten(
                str(state.get("shared_memory") or ""),
                8000,
            ),
            "ai_runs": runs,
            "updated_at": state.get("updated_at") or "",
        }
