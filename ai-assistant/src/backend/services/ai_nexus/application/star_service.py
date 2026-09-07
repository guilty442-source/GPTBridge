from __future__ import annotations

import asyncio
import json
from typing import Any

from ..infrastructure.analytics_repository import number


class InvestmentStarServiceMixin:
    async def _run_local_risk_ai(self, payload: dict[str, Any]) -> dict[str, Any]:
        state = await asyncio.to_thread(self.repository.load_state)
        return await asyncio.to_thread(
            self._run_local_risk_ai_for_state,
            state,
            dict(payload),
        )

    def _run_local_risk_ai_for_state(
        self,
        state: dict[str, Any],
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if not state.get("holdings"):
            return {
                "ok": False,
                "message": "請先讀取 Excel 持股檔。",
                "state": state,
                "diagnostics": self._diagnostics(state),
                "mobile_sync": self._mobile_sync_status(),
            }
        if self.ai_connections is None or not bool(
            getattr(self.ai_connections, "is_configured", False)
        ):
            return {
                "ok": False,
                "queued": False,
                "error_code": "STAR_AI_CHANNEL_NOT_CONNECTED",
                "message": "星澄 AI 通道尚未連線；AI 投資管家不執行舊本機 AI 分析。",
                "state": state,
                "diagnostics": self._diagnostics(state),
                "mobile_sync": self._mobile_sync_status(),
            }
        return self._run_star_analysis_for_state(state, payload)

    def _run_star_analysis_for_state(
        self,
        state: dict[str, Any],
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Production analysis boundary: Star searches and owns analysis.

        AI投資管家 supplies a minimal in-memory portfolio snapshot.  Neither
        Star nor external AI receives access to this tool's database.
        """

        holdings = [
            dict(item)
            for item in state.get("holdings", [])
            if isinstance(item, dict) and number(item.get("quantity"), 0) > 0
        ]
        if not holdings:
            return {
                "ok": False,
                "queued": False,
                "message": "請先讀取含有效數量的持股資料。",
                "state": state,
                "diagnostics": self._diagnostics(state),
                "mobile_sync": self._mobile_sync_status(),
            }
        run_id = str(payload.get("run_id") or "")
        instruction = str(payload.get("instruction") or payload.get("command") or "").strip()
        try:
            explicit_request = not bool(payload.get("trigger"))
            search = self.ai_connections.search_investments_sync(
                holdings,
                allow_external_fallback=(
                    explicit_request and payload.get("allow_external_research", True) is True
                ),
            )
            if not search.get("results"):
                raise RuntimeError(
                    str(search.get("message") or "星澄尚未連線；分析未送出且不排隊。")
                )
            quote_map = {
                (
                    str(item.get("market") or "").upper(),
                    str(item.get("requested_symbol") or "").upper(),
                ): item
                for item in search.get("results", [])
                if isinstance(item, dict) and item.get("trusted")
            }
            analysis_holdings: list[dict[str, Any]] = []
            for holding in holdings:
                compact = {
                    key: holding.get(key)
                    for key in (
                        "symbol",
                        "name",
                        "market",
                        "asset_type",
                        "currency",
                        "quantity",
                        "average_cost",
                        "principal_twd",
                        "current_value_twd",
                        "web_current_value_twd",
                    )
                }
                key = (
                    str(holding.get("market") or "").upper(),
                    str(holding.get("symbol") or "").upper(),
                )
                quote = quote_map.get(key)
                if isinstance(quote, dict):
                    source = next(
                        (entry for entry in quote.get("sources", []) if isinstance(entry, dict)),
                        {},
                    )
                    parameters = quote.get("parameters") if isinstance(quote.get("parameters"), dict) else {}
                    compact.update(
                        {
                            "web_current_price": parameters.get("price"),
                            "market_parameters": dict(parameters),
                            "market_data_source": "星澄即時網路搜尋",
                            "market_data_source_url": source.get("url"),
                            "market_data_updated_at": quote.get("observed_at"),
                            "source_confidence": quote.get("confidence"),
                            "distribution": quote.get("distribution"),
                        }
                    )
                analysis_holdings.append(compact)
            policy = self._investment_policy()
            requested_parameters = payload.get("analysis_parameters")
            if not isinstance(requested_parameters, dict):
                requested_parameters = {}
            analysis_parameters = {
                "position_concentration_percent": requested_parameters.get(
                    "position_concentration_percent",
                    policy.get("max_single_position_percent", 20),
                ),
                "missing_data_warning_percent": requested_parameters.get(
                    "missing_data_warning_percent",
                    5,
                ),
                "instruction": instruction,
            }
            analysis = self.ai_connections.analyze_investments_sync(
                analysis_holdings,
                analysis_parameters,
            )
            if analysis.get("ok") is not True:
                raise RuntimeError(
                    str(analysis.get("message") or "星澄投資分析失敗。")
                )
            discuss_with_external_ai = (
                explicit_request
                and payload.get("discuss_with_external_ai", True) is True
            )
            discussion = (
                self.ai_connections.discuss_analysis_sync(analysis)
                if discuss_with_external_ai
                else {
                    "ok": False,
                    "queued": False,
                    "skipped": True,
                    "message": "背景分析不啟動外部 AI 瀏覽器。",
                    "uses_api": False,
                }
            )
            analysis["market_search"] = {
                "provider": search.get("provider"),
                "searched_at": search.get("searched_at"),
                "requested_count": search.get("requested_count"),
                "updated_count": search.get("updated_count"),
                "error_count": search.get("error_count"),
                "errors": search.get("errors", [])[:20],
            }
            analysis["external_ai_discussion"] = discussion
            warnings = analysis.get("risk_warnings") if isinstance(analysis.get("risk_warnings"), list) else []
            portfolio = analysis.get("portfolio") if isinstance(analysis.get("portfolio"), dict) else {}
            summary = {
                "warning_count": len(warnings),
                "active_holding_count": portfolio.get("active_holding_count", len(holdings)),
                "market_data_coverage_percent": portfolio.get("market_data_coverage_percent", 0),
                "analysis_owner": "星澄",
                "discussion_owner": (
                    "ChatGPT（星澄統整）"
                    if discussion.get("ok")
                    else "等待 ChatGPT"
                    if discussion.get("queued")
                    else "未連線"
                ),
            }
            product_status = {
                "state": "ready" if not warnings else "warning",
                "state_label": "分析完成" if not warnings else "分析完成，有風險提醒",
                "analysis_owner": "星澄",
                "computation_service_owner": "星澄",
                "statistics_service_owner": "星澄",
                "network_search_service_owner": "星澄",
                "market_data_provider": "星澄即時網路搜尋",
                "external_discussion_connected": discussion.get("ok") is True,
                "queue_when_offline": False,
            }
            state_after_run = self.repository.save_xingcheng_result(
                product_status,
                summary,
                warnings,
                None,
                full_analysis=analysis,
                explanation={
                    "mode": "star-analysis",
                    "mode_label": "星澄分析",
                    "text": "市場搜尋與投資分析由星澄執行；外部 AI 僅討論分析結果。",
                },
            )
            prompt = "星澄：搜尋可驗證市場資料並執行投資分析。"
            if instruction:
                prompt += f" 使用者指令：{instruction}"
            if run_id:
                run = self.repository.update_ai_run(
                    run_id,
                    status="completed",
                    content=json.dumps(summary, ensure_ascii=False),
                    error="",
                )
            else:
                run = self.repository.add_ai_run(
                    role="investment_analysis",
                    provider="星澄",
                    prompt=prompt,
                    status="completed",
                    content=json.dumps(summary, ensure_ascii=False),
                    error="",
                )
            return {
                "ok": True,
                "queued": False,
                "message": f"星澄分析完成：{len(warnings)} 項風險提醒。",
                "run": run,
                "summary": summary,
                "product_status": product_status,
                "risk_warnings": warnings,
                "local_risk_ai": analysis,
                "external_ai_discussion": discussion,
                "state": self._state_with_analytics(state_after_run),
                "diagnostics": self._diagnostics(state_after_run),
                "mobile_sync": self._mobile_sync_status(),
            }
        except Exception as exc:
            if run_id:
                run = self.repository.update_ai_run(
                    run_id,
                    status="failed",
                    content="",
                    error=str(exc),
                )
            else:
                run = self.repository.add_ai_run(
                    role="investment_analysis",
                    provider="星澄",
                    prompt="星澄投資分析",
                    status="failed",
                    content="",
                    error=str(exc),
                )
            latest = self.repository.load_state()
            return {
                "ok": False,
                "queued": False,
                "message": str(exc),
                "run": run,
                "state": latest,
                "diagnostics": self._diagnostics(latest),
                "mobile_sync": self._mobile_sync_status(),
            }

    def _schedule_local_risk_ai_background(
        self,
        state: dict[str, Any],
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if self.ai_connections is None or not bool(
            getattr(self.ai_connections, "is_configured", False)
        ):
            return {
                "ok": False,
                "queued": False,
                "error_code": "STAR_AI_CHANNEL_NOT_CONNECTED",
                "message": "星澄 AI 通道尚未連線，未建立背景工作。",
                "state": state,
                "product_status": state.get("xingcheng_product_status"),
            }
        prompt = (
            "星澄服務：持股更新後由星澄取得資料並執行風險監測"
            if payload.get("trigger") == "manual_holding_change"
            else "星澄服務：Excel 匯入後由星澄取得資料並執行風險監測"
        )
        run = self.repository.add_ai_run(
            role="investment_risk_monitor",
            provider="星澄",
            prompt=prompt,
            status="running",
            content="",
            error="",
        )
        state_with_run = self.repository.load_state()
        background_payload = {
            **payload,
            "run_id": run["run_id"],
            "portfolio_signature": self._portfolio_signature(state_with_run),
            "allow_external_research": False,
            "discuss_with_external_ai": False,
        }
        task = asyncio.create_task(
            asyncio.to_thread(
                self._run_local_risk_ai_for_state,
                state_with_run,
                background_payload,
            )
        )
        self._background_jobs.add(task)
        task.add_done_callback(self._background_job_finished)
        accounting = self._schedule_star_accounting_background(
            trigger=str(payload.get("trigger") or "portfolio_change")
        )
        return {
            "ok": True,
            "queued": True,
            "message": "已交由星澄服務處理；投資管家本身不聯網。",
            "run": run,
            "state": state_with_run,
            "product_status": state_with_run.get("xingcheng_product_status"),
            "star_accounting": accounting,
        }

    def _portfolio_signature_matches(self, payload: dict[str, Any]) -> bool:
        expected = str(payload.get("portfolio_signature") or "")
        if not expected:
            return True
        return expected == self._portfolio_signature(self.repository.load_state())

    @staticmethod
    def _portfolio_signature(state: dict[str, Any]) -> str:
        portfolio = state.get("portfolio") if isinstance(state.get("portfolio"), dict) else {}
        return "|".join(
            [
                str(portfolio.get("source_path") or ""),
                str(portfolio.get("imported_at") or ""),
                str(portfolio.get("holding_count") or ""),
                str(portfolio.get("manual_revision") or ""),
                str(portfolio.get("manually_modified_at") or ""),
            ]
        )
