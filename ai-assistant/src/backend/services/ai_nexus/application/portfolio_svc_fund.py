from __future__ import annotations

import asyncio
from typing import Any


class PortfolioSvcFundMixin:
    async def _resolve_fund_identities(self, payload: dict[str, Any]) -> dict[str, Any]:
        state = self.repository.load_state()
        holdings = [dict(item) for item in state.get("holdings", []) if isinstance(item, dict)]
        if self.ai_connections is None:
            raise ValueError("AI投資管家 AI 通道尚未連線，基金辨識未執行且不排隊。")
        star_result = await asyncio.to_thread(
            self.ai_connections.search_investments_sync,
            holdings[: self._int_value(payload.get("limit")) or 50],
        )
        results = [item for item in star_result.get("results", []) if isinstance(item, dict)]
        result_by_key = {
            (
                str(item.get("market") or "").upper(),
                str(item.get("requested_symbol") or "").upper(),
            ): item
            for item in results
        }
        resolved = []
        matched = 0
        for holding in holdings:
            enriched = dict(holding)
            item = result_by_key.get(
                (
                    str(holding.get("market") or "").upper(),
                    str(holding.get("symbol") or "").upper(),
                )
            )
            if item and item.get("resolved_symbol"):
                enriched["fund_quote_symbol"] = str(item["resolved_symbol"]).upper()
                enriched["fund_identity_status"] = "confirmed"
                enriched["fund_identity_confirmation"] = "star-ai-channel"
                matched += 1
            resolved.append(enriched)
        summary = {
            "attempted_count": len(holdings),
            "matched_count": matched,
            "auto_confirmed_count": matched,
            "provider": "ai-assistant",
            "transport": "governance-authenticated-ai-channel",
        }
        saved = self.repository.replace_holdings(
            resolved,
            change={"action": "fund_identity_resolution", "matched": summary.get("matched_count", 0)},
        )
        saved["fund_identity_sync"] = summary
        self.repository.save_state(saved)
        self.analytics_store.audit("fund_identity_resolution", summary)
        self._invalidate_v3_snapshot()
        response = self._state_response(saved)
        response.update(
            {
                "message": (
                    f"共同基金辨識完成：比對 {summary.get('attempted_count', 0)} 筆，"
                    f"找到 {summary.get('matched_count', 0)} 筆，高信心自動確認 "
                    f"{summary.get('auto_confirmed_count', 0)} 筆。"
                ),
                "fund_identity_sync": summary,
            }
        )
        return response

    async def _confirm_fund_identity(self, payload: dict[str, Any]) -> dict[str, Any]:
        holding_id = str(payload.get("holding_id") or "").strip()
        quote_symbol = str(payload.get("quote_symbol") or "").strip().upper()
        if not holding_id or not quote_symbol:
            raise ValueError("缺少基金持股或報價代號")
        state = self.repository.load_state()
        holdings = [dict(item) for item in state.get("holdings", []) if isinstance(item, dict)]
        target = next(
            (item for item in holdings if str(item.get("holding_id") or "") == holding_id),
            None,
        )
        if target is None:
            raise ValueError("找不到共同基金持股")
        candidates = target.get("fund_identity_candidates") if isinstance(target.get("fund_identity_candidates"), list) else []
        candidate = next(
            (
                item
                for item in candidates
                if isinstance(item, dict)
                and str(item.get("symbol") or "").upper() == quote_symbol
            ),
            None,
        )
        target["fund_quote_symbol"] = quote_symbol
        target["fund_candidate_symbol"] = quote_symbol
        target["fund_identity_status"] = "confirmed"
        target["fund_identity_confirmation"] = "manual"
        if isinstance(candidate, dict):
            target["fund_identity_confidence"] = candidate.get("confidence_score")
            target["fund_identity_source"] = candidate.get("source")
            target["fund_identity_source_url"] = candidate.get("source_url")
        saved = self.repository.replace_holdings(
            holdings,
            change={"action": "fund_identity_confirm", "symbol": target.get("symbol")},
        )
        self.analytics_store.audit(
            "fund_identity_confirmed",
            {"holding_id": holding_id, "symbol": target.get("symbol"), "quote_symbol": quote_symbol},
            severity="warning",
        )
        self._invalidate_v3_snapshot()
        response = self._state_response(saved)
        response["message"] = f"已確認基金報價代號 {quote_symbol}。"
        return response
