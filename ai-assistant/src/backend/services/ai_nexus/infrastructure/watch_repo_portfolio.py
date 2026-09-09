from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from .watch_repo_helpers import utc_now


class WatchRepoPortfolioMixin:
    """Portfolio import and manual holding management."""

    def save_portfolio(
        self,
        source_path: Path,
        holdings: list[dict[str, Any]],
        workbook_scan: dict[str, Any] | None = None,
        excel_import_profile: dict[str, Any] | None = None,
        import_fingerprint: str = "",
    ) -> dict[str, Any]:
        with self._state_lock:
            return self._save_portfolio_locked(
                source_path,
                holdings,
                workbook_scan=workbook_scan,
                excel_import_profile=excel_import_profile,
                import_fingerprint=import_fingerprint,
            )

    def _save_portfolio_locked(
        self,
        source_path: Path,
        holdings: list[dict[str, Any]],
        workbook_scan: dict[str, Any] | None = None,
        excel_import_profile: dict[str, Any] | None = None,
        import_fingerprint: str = "",
    ) -> dict[str, Any]:
        state = self.load_state()
        normalized_fingerprint = str(import_fingerprint or "").strip()
        current_portfolio = (
            state.get("portfolio")
            if isinstance(state.get("portfolio"), dict)
            else {}
        )
        if (
            normalized_fingerprint
            and current_portfolio.get("import_fingerprint")
            == normalized_fingerprint
        ):
            return state
        self.create_state_version(state, reason="before_import")
        previous_holdings = [
            item for item in state.get("holdings", []) if isinstance(item, dict)
        ]
        previous_by_key = {
            self._holding_import_key(item): item for item in previous_holdings
        }
        merged_holdings: list[dict[str, Any]] = []
        preserved_fields = (
            "web_current_price",
            "web_current_price_currency",
            "web_current_value_twd",
            "market_data_source",
            "market_data_source_url",
            "market_data_updated_at",
            "dividend_source",
            "dividend_source_url",
            "dividend_updated_at",
            "dividend_status",
            "dividend_frequency",
            "dividend_frequency_label",
            "dividend_frequency_per_year",
            "dividend_frequency_median_days",
            "dividend_frequency_confidence",
            "dividend_frequency_source",
            "external_annual_dividend_per_unit",
            "fund_code",
            "fund_isin",
            "fund_share_class",
            "fund_quote_symbol",
            "fund_candidate_symbol",
            "fund_identity_status",
            "fund_identity_confidence",
            "fund_identity_source",
            "fund_identity_source_url",
            "fund_identity_candidates",
            "fund_identity_checked_at",
            "fund_identity_confirmation",
        )
        for source in holdings:
            item = dict(source)
            previous = previous_by_key.get(self._holding_import_key(item))
            if previous is not None:
                item["holding_id"] = previous.get("holding_id")
                for field in preserved_fields:
                    if field in previous:
                        item[field] = previous[field]
                if (
                    item.get("average_cost") is None
                    and previous.get("average_cost_method")
                    == "principal_twd_huanan_current_fx_estimate"
                ):
                    for field in (
                        "average_cost",
                        "average_cost_currency",
                        "average_cost_method",
                        "average_cost_fx_rate",
                    ):
                        item[field] = previous.get(field)
            merged_holdings.append(item)
        normalized_holdings = self._with_holding_ids(merged_holdings)
        try:
            source_created_at = datetime.fromtimestamp(
                source_path.stat().st_ctime
            ).astimezone().isoformat()
        except OSError:
            source_created_at = ""
        state["portfolio"] = {
            "source_path": str(source_path),
            "file_name": source_path.name,
            "holding_count": len(normalized_holdings),
            "imported_at": utc_now(),
            "import_fingerprint": normalized_fingerprint,
            "source_created_at": source_created_at,
            "manually_modified_at": "",
            "manual_revision": 0,
        }
        state["holdings"] = normalized_holdings
        state["workbook_scan"] = self._compact_workbook_scan(workbook_scan)
        state["workbook_scan_quality"] = self._workbook_scan_quality(workbook_scan)
        state["excel_import_profile"] = (
            {
                **excel_import_profile,
                "source_path": str(source_path),
                "updated_at": utc_now(),
            }
            if isinstance(excel_import_profile, dict)
            else None
        )
        state["xingcheng_product_status"] = None
        state["xingcheng_summary"] = None
        state["xingcheng_risk_warnings"] = []
        state["xingcheng_command_result"] = None
        state["xingcheng_action_plan"] = []
        state["xingcheng_watch_triggers"] = []
        state["xingcheng_confidence"] = None
        state["xingcheng_decision_brief"] = ""
        state["xingcheng_network_context"] = None
        state["xingcheng_analysis_cache"] = None
        state["xingcheng_external_discussion"] = None
        state["xingcheng_explanation"] = None
        state["dividend_sync"] = None
        state["market_quote_sync"] = None
        state["portfolio_memory"] = self._portfolio_memory(normalized_holdings)
        return self.save_state(state)

    @staticmethod
    def _holding_import_key(holding: dict[str, Any]) -> tuple[str, str, str]:
        return (
            str(holding.get("market") or "").strip().upper(),
            str(holding.get("symbol") or "").strip().upper(),
            str(holding.get("source_row") or "").strip(),
        )

    def replace_holdings(
        self,
        holdings: list[dict[str, Any]],
        *,
        change: dict[str, Any],
    ) -> dict[str, Any]:
        with self._state_lock:
            return self._replace_holdings_locked(holdings, change=change)

    def _replace_holdings_locked(
        self,
        holdings: list[dict[str, Any]],
        *,
        change: dict[str, Any],
    ) -> dict[str, Any]:
        state = self.load_state()
        self.create_state_version(
            state,
            reason=str(change.get("action") or "manual_change"),
        )
        normalized_holdings = self._with_holding_ids(holdings)
        portfolio = state.get("portfolio") if isinstance(state.get("portfolio"), dict) else {}
        revision = int(portfolio.get("manual_revision") or 0) + 1
        state["portfolio"] = {
            **portfolio,
            "source_path": str(portfolio.get("source_path") or ""),
            "file_name": str(portfolio.get("file_name") or "手動建立持股"),
            "holding_count": len(normalized_holdings),
            "imported_at": str(portfolio.get("imported_at") or utc_now()),
            "manually_modified_at": utc_now(),
            "manual_revision": revision,
            "last_manual_change": dict(change),
        }
        state["holdings"] = normalized_holdings
        state["xingcheng_product_status"] = None
        state["xingcheng_summary"] = None
        state["xingcheng_risk_warnings"] = []
        state["xingcheng_command_result"] = None
        state["xingcheng_action_plan"] = []
        state["xingcheng_watch_triggers"] = []
        state["xingcheng_confidence"] = None
        state["xingcheng_decision_brief"] = ""
        state["xingcheng_network_context"] = None
        state["xingcheng_analysis_cache"] = None
        state["xingcheng_external_discussion"] = None
        state["xingcheng_explanation"] = None
        state["portfolio_memory"] = self._portfolio_memory(normalized_holdings)
        return self.save_state(state)

    @staticmethod
    def _with_holding_ids(holdings: list[dict[str, Any]]) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        seen: set[str] = set()
        for source in holdings:
            item = dict(source)
            holding_id = str(item.get("holding_id") or "").strip()
            if not holding_id or holding_id in seen:
                holding_id = uuid.uuid4().hex
            item["holding_id"] = holding_id
            seen.add(holding_id)
            output.append(item)
        return output

    @staticmethod
    def _portfolio_memory(holdings: list[dict[str, Any]]) -> str:
        lines = [
            "我的持股狀況：",
            "symbol | name | market | quantity | average_cost | currency",
        ]
        for holding in holdings:
            lines.append(
                " | ".join(
                    [
                        str(holding.get("symbol") or ""),
                        str(holding.get("name") or ""),
                        str(holding.get("market") or ""),
                        str(holding.get("quantity") or 0),
                        str(holding.get("average_cost") or ""),
                        str(holding.get("currency") or ""),
                    ]
                )
            )
        return "\n".join(lines)
