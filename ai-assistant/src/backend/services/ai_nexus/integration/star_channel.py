from __future__ import annotations

from typing import Any


class InvestmentAiConnections:
    """AI Investment Manager's governed connection to Star only."""

    def __init__(self) -> None:
        self._client: Any | None = None

    def bind_channel(self, channel: Any) -> None:
        from governance_rule.permission_directory.registries.permissions.tool_routes import (
            authorize_ai_route,
            tool_actor,
        )
        from shared_layer import GovernedRequestClient

        self._client = GovernedRequestClient(
            channel,
            tool_actor("ai-assistant"),
            authorize_ai_route,
            transport="ai-channel",
        )

    @property
    def is_configured(self) -> bool:
        return self._client is not None

    def status(self) -> dict[str, Any]:
        configured = self.is_configured
        return {
            "ok": True,
            "transport": "governance-authenticated-ai-channel",
            "queue_when_offline": False,
            "database_shared": False,
            "investment_manager_network": "disabled",
            "service_owner": "local-ai",
            "highest_authority": "governance-rule",
            "channel_top_level_tool": "local-ai",
            "roles": {
                "investment_manager": "offline-portfolio-state-and-settings",
                "local_ai": "exclusive-market-search-investment-analysis-service-owner",
                "external_ai": "unavailable-to-investment-manager",
            },
            "peers": {
                "local_ai": {"name": "星澄", "configured": configured},
                "external_ai": {
                    "name": "外部 AI 協作",
                    "configured": False,
                    "permission": "PERMISSION_DENIED",
                },
            },
        }

    def _not_ready(self) -> dict[str, Any]:
        return {
            "ok": False,
            "queued": False,
            "transport": "ai-channel",
            "error_code": "AI_CHANNEL_NOT_CONNECTED",
            "message": "AI 通道尚未連線",
        }

    async def consult(self, prompt: str, scope: str = "local") -> dict[str, Any]:
        if not prompt.strip():
            return {"ok": False, "message": "prompt is required"}
        if scope not in {"local", "star"}:
            return {
                "ok": False,
                "queued": False,
                "error_code": "PERMISSION_DENIED",
                "message": "PERMISSION_DENIED",
            }
        if self._client is None:
            return self._not_ready()
        result = await self._client.request(
            "local-ai", "local_ai_infer", {"prompt": prompt}, timeout_seconds=200
        )
        return {
            "ok": result.get("ok") is True,
            "queued": False,
            "results": {"local_ai": result},
        }

    def search_investments_sync(
        self,
        holdings: list[dict[str, Any]],
        *,
        allow_external_fallback: bool = False,
    ) -> dict[str, Any]:
        del allow_external_fallback
        if self._client is None:
            return self._not_ready()
        return self._client.request_sync(
            "local-ai",
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
                            "fund_code",
                            "fund_isin",
                            "isin",
                        )
                    }
                    for item in holdings
                    if isinstance(item, dict)
                ],
                "require_all": False,
                # AI Investment Manager is not authorized to request external AI.
                "allow_external_fallback": False,
                "request_origin": "offline-ai-investment-manager",
            },
            timeout_seconds=200,
        )

    def analyze_investments_sync(
        self,
        holdings: list[dict[str, Any]],
        analysis_parameters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self._client is None:
            return self._not_ready()
        return self._client.request_sync(
            "local-ai",
            "local_ai_analyze_investments",
            {
                "holdings": holdings,
                "analysis_parameters": dict(analysis_parameters or {}),
                "request_origin": "offline-ai-investment-manager",
            },
            timeout_seconds=200,
        )

    def manage_accounting_sync(
        self,
        reconciliation: dict[str, Any],
        ledger_summary: dict[str, Any],
        *,
        trigger: str,
    ) -> dict[str, Any]:
        if self._client is None:
            return self._not_ready()
        return self._client.request_sync(
            "local-ai",
            "local_ai_manage_investment_accounting",
            {
                "reconciliation": dict(reconciliation),
                "ledger_summary": dict(ledger_summary),
                "autonomous": True,
                "trigger": trigger,
                "request_origin": "offline-ai-investment-manager",
            },
            timeout_seconds=120,
        )

    @staticmethod
    def _discussion_snapshot(analysis: dict[str, Any]) -> dict[str, Any]:
        portfolio = analysis.get("portfolio")
        confidence = analysis.get("confidence")
        return {
            "portfolio": dict(portfolio) if isinstance(portfolio, dict) else {},
            "model_results": {
                str(key): dict(value)
                for key, value in list(
                    (
                        analysis.get("model_results")
                        if isinstance(analysis.get("model_results"), dict)
                        else {}
                    ).items()
                )[:8]
                if isinstance(value, dict)
            },
            "model_execution": dict(analysis.get("model_execution") or {}),
            "evidence": [
                dict(item)
                for item in analysis.get("evidence", [])[:30]
                if isinstance(item, dict)
            ],
            "risk_warnings": [
                dict(item)
                for item in analysis.get("risk_warnings", [])[:30]
                if isinstance(item, dict)
            ],
            "action_plan": [
                dict(item)
                for item in analysis.get("action_plan", [])[:30]
                if isinstance(item, dict)
            ],
            "watch_triggers": [
                dict(item)
                for item in analysis.get("watch_triggers", [])[:30]
                if isinstance(item, dict)
            ],
            "confidence": dict(confidence) if isinstance(confidence, dict) else {},
            "facts_locked": True,
            "database_shared": False,
        }

    def discuss_analysis_sync(self, analysis: dict[str, Any]) -> dict[str, Any]:
        if self._client is None:
            return self._not_ready()
        return self._client.request_sync(
            "local-ai",
            "local_ai_discuss_investment_analysis",
            {
                "analysis_snapshot": self._discussion_snapshot(analysis),
                "request_origin": "offline-ai-investment-manager",
            },
            timeout_seconds=200,
        )

    def list_star_memory_sync(
        self,
        *,
        include_inactive: bool = False,
        limit: int = 100,
    ) -> dict[str, Any]:
        if self._client is None:
            return self._not_ready()
        return self._client.request_sync(
            "local-ai",
            "local_ai_memory_list",
            {
                "include_inactive": include_inactive,
                "limit": max(1, min(500, int(limit))),
                "request_origin": "offline-ai-investment-manager",
            },
            timeout_seconds=45,
        )

    def review_star_memory_sync(
        self,
        memory_id: str,
        *,
        action: str,
        reviewer: str,
        reason: str = "",
    ) -> dict[str, Any]:
        if self._client is None:
            return self._not_ready()
        return self._client.request_sync(
            "local-ai",
            "local_ai_memory_review",
            {
                "memory_id": memory_id,
                "action": action,
                "reviewer": reviewer,
                "reason": reason,
                "request_origin": "offline-ai-investment-manager",
            },
            timeout_seconds=45,
        )
