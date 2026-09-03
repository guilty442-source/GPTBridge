from __future__ import annotations

import asyncio
import time
from typing import Any

from .investment_accounting import coordinate_investment_accounting
from .investment_analysis import analyze_investments


class InvestmentChannelMixin:
    async def _handle_tune_mobile(self, command: str, payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        if command == "xingcheng_tune_investment_parameters":
            result = await self._tune_investment_parameters(payload)
            self._identify_model(result, self.models.primary)
            return "xingcheng_tune_investment_parameters_result", result
        if command in {
            "xingcheng_mobile_get_investment_snapshot",
            "xingcheng_mobile_submit_investment_instruction",
        }:
            if self._ai_channel_client is None:
                return f"{command}_result", {
                    "ok": False,
                    "queued": False,
                    "error_code": "AI_CHANNEL_NOT_CONNECTED",
                    "message": "星澄 AI 通道尚未連線。",
                }
            target_command = (
                "investment_mobile_get_snapshot"
                if command == "xingcheng_mobile_get_investment_snapshot"
                else "investment_mobile_submit_instruction"
            )
            result = await self._ai_channel_client.request(
                "ai-assistant", target_command, dict(payload), timeout_seconds=45
            )
            result["connection_coordinator"] = "xingcheng"
            result["governance_checked"] = True
            return f"{command}_result", result

    async def _handle_investments(self, command: str, payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        if command == "xingcheng_search_investments":
            result, cache_hit, cache_ttl = await self._search_market_data(dict(payload))
            self._identify_model(result, self.models.for_command(command))
            result["cache"] = {
                "hit": cache_hit,
                "ttl_seconds": cache_ttl,
                "coalesced": cache_hit and payload.get("force_refresh") is True,
            }
            if result.get("errors"):
                result["external_research"] = {
                    "configured": False,
                    "enabled": False,
                    "external_ai_used": False,
                    "policy": "local-market-sources-and-ollama-only",
                }
            if not cache_hit:
                search_repository = self._repository_for(
                    self.models.for_command(command)
                )
                await asyncio.to_thread(
                    search_repository.record_market_search, dict(payload), result
                )
            return "xingcheng_search_investments_result", result
        if command == "xingcheng_analyze_investments":
            analysis_payload = dict(payload)
            requested_parameters = analysis_payload.get("analysis_parameters")
            if not isinstance(requested_parameters, dict):
                requested_parameters = {}
            governed_parameters = self.investment_repository.investment_parameter_values()
            analysis_payload["analysis_parameters"] = {
                "position_concentration_percent": governed_parameters[
                    "max_single_position_percent"
                ],
                "missing_data_warning_percent": governed_parameters[
                    "missing_data_warning_percent"
                ],
                **requested_parameters,
            }
            self._runtime_metrics["analysis_request_count"] = int(
                self._runtime_metrics["analysis_request_count"]
            ) + 1
            self._runtime_metrics["last_activity_at"] = time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
            )
            analysis_started = time.perf_counter()
            result = await asyncio.to_thread(analyze_investments, analysis_payload)
            self._record_latency("analysis", analysis_started)
            profile = self.models.for_command(command)
            self._identify_model(result, profile)
            self._repository_for(profile).record(profile.model_id, payload, result)
            return "xingcheng_analyze_investments_result", result
        if command == "xingcheng_discuss_investment_analysis":
            snapshot = payload.get("analysis_snapshot")
            if not isinstance(snapshot, dict):
                return "xingcheng_discuss_investment_analysis_result", {
                    "ok": False,
                    "queued": False,
                    "error_code": "INVALID_ANALYSIS_SNAPSHOT",
                    "message": "缺少星澄投資分析快照。",
                }
            generated = await asyncio.to_thread(
                self.transformer_runtime.generate,
                prompt="統籌並核對這份投資分析，僅根據快照提出可驗證結論。",
                intent="analysis",
                model_role="local-investment-analysis-coordinator",
                output={"analysis": dict(snapshot), "response": ""},
                reasoning_effort="high",
                reasoning_pipeline=True,
            )
            result = {
                **generated,
                "response": str(generated.get("text") or ""),
                "analysis_owner": "xingcheng",
                "discussion_owner": self.FINAL_COORDINATOR_MODEL,
                "discussion_requested_by": "local-ollama-router",
                "direct_investment_manager_response": False,
                "external_ai_used": False,
                "transport": "ollama-loopback-only",
            }
            return "xingcheng_discuss_investment_analysis_result", result
        if command == "xingcheng_manage_investment_accounting":
            result = await asyncio.to_thread(
                coordinate_investment_accounting,
                dict(payload),
            )
            result["coordination"] = "star-main-model-mediated"
            result["assigned_specialists"] = [
                self.models.INVESTMENT.model_id,
                self.models.MATHEMATICAL.model_id,
            ]
            self._identify_model(result, self.models.primary)
            self._repository_for(self.models.primary).record(
                self.models.primary.model_id,
                {"task": "investment-accounting", "trigger": payload.get("trigger")},
                result,
            )
            return "xingcheng_manage_investment_accounting_result", result
