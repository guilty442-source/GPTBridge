from __future__ import annotations

import asyncio
from typing import Any

from ..infrastructure.analytics_repository import market_session_status, number


class PortfolioSvcSyncMixin:
    async def _sync_intelligence(self, payload: dict[str, Any]) -> dict[str, Any]:
        state = self.repository.load_state()
        holdings = [item for item in state.get("holdings", []) if isinstance(item, dict)]
        if not holdings:
            raise ValueError("請先讀取持股檔，再同步市場情報")
        del payload
        if self.ai_connections is None:
            raise ValueError("AI投資管家 AI 通道尚未連線，市場情報未執行且不排隊。")
        star_result = await asyncio.to_thread(
            self.ai_connections.search_investments_sync, holdings
        )
        if star_result.get("ok") is not True:
            raise ValueError(str(star_result.get("message") or "AI投資管家市場情報服務失敗。"))
        result = {
            "provider": "ai-assistant",
            "service_owner": "AI投資管家",
            "transport": "governance-authenticated-ai-channel",
            "requested_count": star_result.get("requested_count", len(holdings)),
            "updated_count": star_result.get("updated_count", 0),
            "error_count": star_result.get("error_count", 0),
            "errors": star_result.get("errors", []),
            "searched_at": star_result.get("searched_at"),
            "corporate_actions_added": 0,
        }
        self._invalidate_v3_snapshot()
        response = self._state_response(state)
        response.update(result)
        return response

    async def _sync_open_markets(self, _payload: dict[str, Any]) -> dict[str, Any]:
        state = self.repository.load_state()
        holdings = [item for item in state.get("holdings", []) if isinstance(item, dict)]
        sessions = market_session_status()
        held_markets = {
            str(item.get("market") or "").strip().upper()
            for item in holdings
            if number(item.get("quantity"), 0) > 0
        }
        open_markets = [
            market
            for market in sessions["open_markets"]
            if market in held_markets
        ]
        searchable_holdings = [
            item
            for item in holdings
            if number(item.get("quantity"), 0) > 0
            and (
                str(item.get("market") or "").strip().upper() in open_markets
                or str(item.get("asset_type") or "").strip().upper() == "FUND"
                or str(item.get("market") or "").strip().upper() == "FUND"
            )
        ]
        if not holdings or not searchable_holdings:
            return {
                "ok": True,
                "not_modified": True,
                "state_revision": self._state_revision(state),
                "market_sessions": sessions,
                "market_quote_sync": {
                    "status": "market_closed" if holdings else "no_portfolio",
                    "open_markets": open_markets,
                    "requested_count": 0,
                    "updated_count": 0,
                },
            }
        if self.ai_connections is not None:
            star_result = await asyncio.to_thread(
                self.ai_connections.search_investments_sync,
                searchable_holdings,
            )
            if not star_result.get("results"):
                raise ValueError(
                    str(star_result.get("message") or "AI投資管家尚未連線，報價未送出且不排隊。")
                )
            quotes: list[dict[str, Any]] = []
            bars: list[dict[str, Any]] = []
            for item in star_result.get("results", []):
                if not isinstance(item, dict) or not item.get("trusted"):
                    continue
                parameters = item.get("parameters") if isinstance(item.get("parameters"), dict) else {}
                price = number(parameters.get("price"), 0)
                if price <= 0:
                    continue
                source = next(
                    (entry for entry in item.get("sources", []) if isinstance(entry, dict)),
                    {},
                )
                quote = {
                    "symbol": str(item.get("requested_symbol") or "").upper(),
                    "market": str(item.get("market") or "").upper(),
                    "current_price": price,
                    "currency": str(item.get("currency") or "").upper(),
                    "observed_at": str(item.get("observed_at") or ""),
                    "source_url": str(source.get("url") or ""),
                    "resolved_symbol": str(item.get("resolved_symbol") or ""),
                    "quote_kind": str(item.get("quote_kind") or "market_price"),
                }
                quotes.append(quote)
                bars.append(
                    {
                        "symbol": quote["symbol"],
                        "observed_at": quote["observed_at"],
                        "open": price,
                        "high": price,
                        "low": price,
                        "close": price,
                        "volume": None,
                        "currency": quote["currency"],
                        "provider": "star-web-search",
                        "verified": True,
                    }
                )
            prices_added = self.analytics_store.add_price_bars(bars) if bars else 0
            result = {
                "provider": "內建瀏覽器即時網路搜尋",
                "updated_at": star_result.get("searched_at"),
                "data_as_of": max((str(item.get("observed_at") or "") for item in quotes), default=""),
                "open_markets": open_markets,
                "requested_count": star_result.get("requested_count", len(searchable_holdings)),
                "updated_count": len(quotes),
                "coverage_percent": round(len(quotes) / len(searchable_holdings) * 100, 2) if searchable_holdings else 100.0,
                "prices_added": prices_added,
                "quotes": quotes,
                "error_count": star_result.get("error_count", 0),
                "errors": star_result.get("errors", []),
                "methodology": "內建瀏覽器搜尋具來源與日期的市價或基金淨值",
                "limitations": ["共同基金淨值不是盤中成交價。", "無法驗證的結果不寫入。"],
            }
        else:
            raise ValueError("AI投資管家 AI 通道尚未連線，報價未執行且不排隊。")
        quote_map = {
            (
                str(item.get("market") or "").upper(),
                str(item.get("symbol") or "").upper(),
            ): item
            for item in result.get("quotes", [])
            if isinstance(item, dict)
        }
        if quote_map:
            fx_cache: dict[str, dict[str, Any] | None] = {}
            updated_holdings: list[dict[str, Any]] = []
            for holding in holdings:
                enriched = dict(holding)
                key = (
                    str(holding.get("market") or "").upper(),
                    str(holding.get("symbol") or "").upper(),
                )
                quote = quote_map.get(key)
                if isinstance(quote, dict):
                    current_price = number(quote.get("current_price"), 0)
                    source_currency = str(
                        quote.get("currency") or holding.get("currency") or "TWD"
                    ).upper()
                    enriched["web_current_price"] = current_price
                    enriched["web_current_price_currency"] = source_currency
                    enriched["market_data_source"] = str(
                        result.get("provider") or "內建瀏覽器即時網路搜尋"
                    )
                    enriched["market_data_source_url"] = str(
                        quote.get("source_url") or ""
                    )
                    enriched["market_data_updated_at"] = str(
                        quote.get("observed_at") or ""
                    )
                    if (
                        str(holding.get("asset_type") or "").upper() == "FUND"
                        and quote.get("resolved_symbol")
                    ):
                        enriched["fund_quote_symbol"] = str(
                            quote.get("resolved_symbol") or ""
                        ).upper()
                        enriched["fund_identity_status"] = "confirmed"
                    if source_currency not in fx_cache:
                        fx_cache[source_currency] = self.v3.fx_rate(
                            source_currency,
                            "TWD",
                        )
                    fx_quote = fx_cache[source_currency]
                    if current_price > 0 and fx_quote and number(fx_quote.get("rate"), 0) > 0:
                        enriched["web_current_value_twd"] = round(
                            current_price
                            * number(holding.get("quantity"), 0)
                            * number(fx_quote.get("rate")),
                            4,
                        )
                    principal_amount = number(
                        holding.get("principal_amount"),
                        number(holding.get("average_cost"), 0)
                        * number(holding.get("quantity"), 0),
                    )
                    principal_currency = str(
                        holding.get("principal_currency")
                        or holding.get("currency")
                        or "TWD"
                    ).upper()
                    if principal_currency not in fx_cache:
                        fx_cache[principal_currency] = self.v3.fx_rate(
                            principal_currency,
                            "TWD",
                        )
                    principal_fx = fx_cache[principal_currency]
                    if principal_amount > 0 and principal_fx and number(principal_fx.get("rate"), 0) > 0:
                        enriched["principal_amount"] = principal_amount
                        enriched["principal_currency"] = principal_currency
                        enriched["principal_twd"] = round(
                            principal_amount * number(principal_fx.get("rate")),
                            4,
                        )
                updated_holdings.append(enriched)
            market_quote_sync = {
                "provider": result.get("provider"),
                "updated_at": result.get("updated_at"),
                "open_markets": open_markets,
                "requested_count": result.get("requested_count", 0),
                "updated_count": result.get("updated_count", 0),
                "error_count": result.get("error_count", 0),
            }
            self._merge_synchronized_holdings(
                holdings,
                updated_holdings,
                sync_metadata={"market_quote_sync": market_quote_sync},
                sync_owned_fields={
                    "web_current_price",
                    "web_current_price_currency",
                    "web_current_value_twd",
                    "market_data_source",
                    "market_data_source_url",
                    "market_data_updated_at",
                    "fund_quote_symbol",
                    "fund_identity_status",
                },
            )
        self._invalidate_v3_snapshot()
        response = self._state_response(self.repository.load_state())
        response.update(
            {
                "message": (
                    f"內建瀏覽器報價已更新 "
                    f"{result.get('updated_count', 0)} 筆。"
                ),
                "market_sessions": sessions,
                "market_quote_sync": result,
            }
        )
        return response
