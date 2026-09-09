from __future__ import annotations

from typing import Any

from .watch_repo_utils import WatchRepoUtilsMixin


class WatchRepoXingchengMixin:
    """Xingcheng (local AI) analysis result persistence and compaction."""

    def save_xingcheng_result(
        self,
        product_status: dict[str, Any] | None,
        summary: dict[str, Any] | None,
        risk_warnings: list[dict[str, Any]] | None,
        command_result: dict[str, Any] | None,
        *,
        full_analysis: dict[str, Any] | None = None,
        explanation: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self._state_lock:
            return self._save_xingcheng_result_locked(
                product_status,
                summary,
                risk_warnings,
                command_result,
                full_analysis=full_analysis,
                explanation=explanation,
            )

    def _save_xingcheng_result_locked(
        self,
        product_status: dict[str, Any] | None,
        summary: dict[str, Any] | None,
        risk_warnings: list[dict[str, Any]] | None,
        command_result: dict[str, Any] | None,
        *,
        full_analysis: dict[str, Any] | None = None,
        explanation: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        state = self.load_state()
        state["xingcheng_product_status"] = (
            dict(product_status) if isinstance(product_status, dict) else None
        )
        state["xingcheng_summary"] = dict(summary) if isinstance(summary, dict) else None
        state["xingcheng_risk_warnings"] = list(risk_warnings or [])[:20]
        compact_command = self._compact_command_result(command_result)
        state["xingcheng_command_result"] = compact_command
        state["xingcheng_network_context"] = self._compact_network_context(
            product_status,
            compact_command,
        )
        state["xingcheng_action_plan"] = (
            list(compact_command.get("action_plan", []))[:8]
            if isinstance(compact_command, dict)
            else []
        )
        state["xingcheng_watch_triggers"] = (
            list(compact_command.get("watch_triggers", []))[:12]
            if isinstance(compact_command, dict)
            else []
        )
        state["xingcheng_confidence"] = (
            dict(compact_command.get("confidence_summary", {}))
            if isinstance(compact_command, dict)
            and isinstance(compact_command.get("confidence_summary"), dict)
            else None
        )
        state["xingcheng_decision_brief"] = (
            str(compact_command.get("decision_brief") or "")
            if isinstance(compact_command, dict)
            else ""
        )
        state["xingcheng_analysis_cache"] = self._compact_analysis_cache(full_analysis)
        discussion = (
            full_analysis.get("external_ai_discussion")
            if isinstance(full_analysis, dict)
            and isinstance(full_analysis.get("external_ai_discussion"), dict)
            else None
        )
        state["xingcheng_external_discussion"] = (
            {
                "ok": discussion.get("ok") is True,
                "queued": discussion.get("queued") is True,
                "provider": str(discussion.get("provider") or ""),
                "status": str(discussion.get("status") or ""),
                "content": str(discussion.get("content") or "")[:12000],
                "message": str(discussion.get("message") or ""),
                "response_recipient": str(
                    discussion.get("response_recipient") or "xingcheng"
                ),
                "transport": str(discussion.get("transport") or ""),
            }
            if discussion is not None
            else None
        )
        state["xingcheng_explanation"] = (
            dict(explanation) if isinstance(explanation, dict) else None
        )
        return self.save_state(state)

    @staticmethod
    def _compact_analysis_cache(
        analysis: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if not isinstance(analysis, dict):
            return None
        holdings = analysis.get("holdings") if isinstance(analysis.get("holdings"), list) else []
        compact_holdings: list[dict[str, Any]] = []
        keep_fields = {
            "symbol",
            "name",
            "market",
            "asset_type",
            "quantity",
            "average_cost",
            "currency",
            "status",
            "quote",
            "market_status",
            "market_value",
            "cost_basis",
            "unrealized_pnl",
            "unrealized_pnl_percent",
            "quote_validation",
            "trusted_quote",
            "data_confidence",
            "analysis_fingerprint",
        }
        for source in holdings:
            if isinstance(source, dict):
                compact_holdings.append(
                    {key: value for key, value in source.items() if key in keep_fields}
                )
        return {
            "generated_at": str(analysis.get("generated_at") or ""),
            "holdings": compact_holdings,
        }

    @staticmethod
    def _compact_command_result(
        command_result: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        if not isinstance(command_result, dict):
            return None
        return {
            "intent": command_result.get("intent"),
            "sections": command_result.get("sections", []),
            "symbols": command_result.get("symbols", []),
            "matched_symbols": command_result.get("matched_symbols", []),
            "missing_symbols": command_result.get("missing_symbols", []),
            "portfolio_score": command_result.get("portfolio_score"),
            "portfolio_rating": command_result.get("portfolio_rating"),
            "risk_level": command_result.get("risk_level"),
            "network_context": WatchRepoXingchengMixin._compact_network_context(
                None,
                command_result,
            ),
            "next_actions": command_result.get("next_actions", []),
            "confidence_summary": command_result.get("confidence_summary"),
            "action_plan": list(command_result.get("action_plan", []))[:8]
            if isinstance(command_result.get("action_plan"), list)
            else [],
            "watch_triggers": list(command_result.get("watch_triggers", []))[:12]
            if isinstance(command_result.get("watch_triggers"), list)
            else [],
            "decision_brief": WatchRepoUtilsMixin._shorten(
                str(command_result.get("decision_brief") or ""),
                1000,
            ),
            "text": WatchRepoUtilsMixin._shorten(
                str(command_result.get("text") or ""),
                4000,
            ),
        }

    @staticmethod
    def _compact_network_context(
        product_status: dict[str, Any] | None,
        command_result: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        source: dict[str, Any] | None = None
        if isinstance(product_status, dict) and isinstance(
            product_status.get("network_context"),
            dict,
        ):
            source = product_status["network_context"]
        elif isinstance(command_result, dict) and isinstance(
            command_result.get("network_context"),
            dict,
        ):
            source = command_result["network_context"]
        if not isinstance(source, dict):
            return None
        return {
            "enabled": bool(source.get("enabled")),
            "mode": str(source.get("mode") or ""),
            "mode_label": str(source.get("mode_label") or ""),
            "health": str(source.get("health") or ""),
            "health_label": str(source.get("health_label") or ""),
            "policy": WatchRepoUtilsMixin._shorten(str(source.get("policy") or ""), 500),
            "quote_provider_count": WatchRepoUtilsMixin._int_value(
                source.get("quote_provider_count")
            ),
            "quote_providers": WatchRepoUtilsMixin._compact_string_list(
                source.get("quote_providers"),
                8,
            ),
            "successful_providers": WatchRepoUtilsMixin._compact_string_list(
                source.get("successful_providers"),
                8,
            ),
            "failed_providers": WatchRepoUtilsMixin._compact_string_list(
                source.get("failed_providers"),
                8,
            ),
            "attempt_count": WatchRepoUtilsMixin._int_value(source.get("attempt_count")),
            "holding_count": WatchRepoUtilsMixin._int_value(source.get("holding_count")),
            "quoted_count": WatchRepoUtilsMixin._int_value(source.get("quoted_count")),
            "verified_quote_count": WatchRepoUtilsMixin._int_value(
                source.get("verified_quote_count")
            ),
            "cross_checked_count": WatchRepoUtilsMixin._int_value(
                source.get("cross_checked_count")
            ),
            "single_source_count": WatchRepoUtilsMixin._int_value(
                source.get("single_source_count")
            ),
            "untrusted_quote_count": WatchRepoUtilsMixin._int_value(
                source.get("untrusted_quote_count")
            ),
            "failed_quote_count": WatchRepoUtilsMixin._int_value(
                source.get("failed_quote_count")
            ),
            "validation_issue_count": WatchRepoUtilsMixin._int_value(
                source.get("validation_issue_count")
            ),
            "divergence_count": WatchRepoUtilsMixin._int_value(
                source.get("divergence_count")
            ),
            "stale_quote_count": WatchRepoUtilsMixin._int_value(
                source.get("stale_quote_count")
            ),
            "symbol_mismatch_count": WatchRepoUtilsMixin._int_value(
                source.get("symbol_mismatch_count")
            ),
            "currency_mismatch_count": WatchRepoUtilsMixin._int_value(
                source.get("currency_mismatch_count")
            ),
            "validation_issues": WatchRepoXingchengMixin._compact_validation_issues(
                source.get("validation_issues"),
                12,
            ),
            "quote_gap_count": WatchRepoUtilsMixin._int_value(
                source.get("quote_gap_count")
            ),
            "quote_gaps": WatchRepoXingchengMixin._compact_quote_gaps(
                source.get("quote_gaps"),
                20,
            ),
            "coverage_percent": source.get("coverage_percent"),
            "coverage_label": str(source.get("coverage_label") or ""),
        }

    @staticmethod
    def _compact_quote_gaps(value: Any, limit: int) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        output: list[dict[str, Any]] = []
        for item in value:
            if not isinstance(item, dict):
                continue
            output.append(
                {
                    "symbol": str(item.get("symbol") or ""),
                    "name": WatchRepoUtilsMixin._shorten(
                        str(item.get("name") or ""),
                        120,
                    ),
                    "market": str(item.get("market") or ""),
                    "status": str(item.get("status") or ""),
                    "reason": WatchRepoUtilsMixin._shorten(
                        str(item.get("reason") or ""),
                        160,
                    ),
                    "detail": WatchRepoUtilsMixin._shorten(
                        str(item.get("detail") or ""),
                        260,
                    ),
                    "failed_providers": WatchRepoUtilsMixin._compact_string_list(
                        item.get("failed_providers"),
                        8,
                    ),
                    "attempt_count": WatchRepoUtilsMixin._int_value(
                        item.get("attempt_count")
                    ),
                    "action": WatchRepoUtilsMixin._shorten(
                        str(item.get("action") or ""),
                        240,
                    ),
                }
            )
            if len(output) >= limit:
                break
        return output

    @staticmethod
    def _compact_validation_issues(value: Any, limit: int) -> list[dict[str, str]]:
        if not isinstance(value, list):
            return []
        output: list[dict[str, str]] = []
        for item in value:
            if not isinstance(item, dict):
                continue
            output.append(
                {
                    "symbol": str(item.get("symbol") or ""),
                    "severity": str(item.get("severity") or ""),
                    "code": str(item.get("code") or ""),
                    "title": WatchRepoUtilsMixin._shorten(
                        str(item.get("title") or ""),
                        120,
                    ),
                    "detail": WatchRepoUtilsMixin._shorten(
                        str(item.get("detail") or ""),
                        240,
                    ),
                }
            )
            if len(output) >= limit:
                break
        return output
