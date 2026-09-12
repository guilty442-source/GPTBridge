from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from ..infrastructure.analytics_repository import number


def local_device_now() -> datetime:
    return datetime.now(timezone.utc)


class PortfolioSvcDividendsMixin:
    async def _sync_dividends(self, _payload: dict[str, Any]) -> dict[str, Any]:
        state = self.repository.load_state()
        holdings = [
            dict(item) for item in state.get("holdings", []) if isinstance(item, dict)
        ]
        if not holdings:
            raise ValueError("請先讀取持股檔，再由內建瀏覽器搜尋配息")
        currencies = [
            str(currency or "")
            for item in holdings
            for currency in (
                item.get("currency"),
                item.get("principal_currency"),
            )
        ]
        fx_result = {
            "fx_provider": "ai-assistant",
            "fx_service_owner": "投資管家",
            "fx_requested_currencies": sorted(set(currencies)),
        }
        if self.ai_connections is not None:
            star_result = await asyncio.to_thread(
                self.ai_connections.search_investments_sync,
                [item for item in holdings if number(item.get("quantity"), 0) > 0],
            )
            if not star_result.get("results"):
                raise ValueError(
                    str(star_result.get("message") or "投資管家尚未連線，配息搜尋未送出且不排隊。")
                )
            star_updates: list[dict[str, Any]] = []
            for item in star_result.get("results", []):
                if not isinstance(item, dict) or not item.get("trusted"):
                    continue
                parameters = item.get("parameters") if isinstance(item.get("parameters"), dict) else {}
                distribution = item.get("distribution") if isinstance(item.get("distribution"), dict) else {}
                source = next(
                    (entry for entry in item.get("sources", []) if isinstance(entry, dict)),
                    {},
                )
                market = str(item.get("market") or "").upper()
                requested_symbol = str(item.get("requested_symbol") or "").upper()
                events = [
                    {
                        "occurred_at": event.get("observed_at") or event.get("record_date"),
                        "amount_per_unit": event.get("amount_per_unit"),
                        "source_url": event.get("source_url"),
                        "title": event.get("title"),
                    }
                    for event in item.get("distribution_events", [])
                    if isinstance(event, dict)
                ]
                trailing = number(parameters.get("annual_distribution_per_unit"), 0)
                star_updates.append(
                    {
                        "symbol": requested_symbol,
                        "market": market,
                        "requested_symbol": str(item.get("resolved_symbol") or requested_symbol),
                        "currency": str(item.get("currency") or "").upper(),
                        "name": str(item.get("name") or ""),
                        "instrument_type": "MUTUALFUND" if item.get("asset_type") == "FUND" else str(item.get("asset_type") or "").upper(),
                        "exchange_name": "基金資訊觀測站" if item.get("asset_type") == "FUND" else "",
                        "current_price": number(parameters.get("price"), 0),
                        "trailing_annual_dividend_per_unit": trailing,
                        "annual_dividend_yield_percent": parameters.get("distribution_yield_percent"),
                        "event_count": len(events),
                        "events": events,
                        "dividend_frequency": str(distribution.get("frequency") or "unknown"),
                        "dividend_frequency_label": str(distribution.get("frequency_label") or "待累積資料"),
                        "dividend_frequency_per_year": distribution.get("frequency_per_year"),
                        "dividend_frequency_confidence": distribution.get("frequency_confidence"),
                        "source": "內建瀏覽器即時網路搜尋",
                        "source_url": str(source.get("url") or ""),
                        "updated_at": str(item.get("observed_at") or star_result.get("searched_at") or ""),
                        "status": "updated" if trailing > 0 else "no_distribution" if distribution.get("frequency") == "none" else "distribution_evidence_only" if events or distribution.get("frequency") not in {None, "", "unknown"} else "no_external_dividend",
                        "official_code": str(item.get("official_code") or ""),
                    }
                )
            dividend_result = {
                "requested_count": star_result.get("requested_count", 0),
                "updated_count": sum(item.get("status") in {"updated", "distribution_evidence_only", "no_distribution"} for item in star_updates),
                "no_distribution_count": sum(item.get("status") == "no_distribution" for item in star_updates),
                "no_dividend_count": sum(item.get("status") in {"no_distribution", "no_external_dividend"} for item in star_updates),
                "error_count": star_result.get("error_count", 0),
                "updates": star_updates,
                "errors": star_result.get("errors", []),
                "provider": "內建瀏覽器即時網路搜尋",
                "updated_at": star_result.get("searched_at"),
                "coverage_percent": round(len(star_updates) / max(1, int(star_result.get("requested_count") or 0)) * 100, 2),
                "methodology": "內建瀏覽器搜尋公開市場配息事件與官方基金配息公告",
                "limitations": ["基金公告未揭露可驗證金額時只保存公告與頻率，不推造配息金額。"],
            }
        else:
            raise ValueError("投資管家 AI 通道尚未連線，配息搜尋未執行且不排隊。")
        updates = {
            (str(item.get("market") or ""), str(item.get("symbol") or "")): item
            for item in dividend_result.get("updates", [])
            if isinstance(item, dict)
        }
        updated_holdings: list[dict[str, Any]] = []
        event_payloads: list[dict[str, Any]] = []
        for holding in holdings:
            enriched = self._enrich_holding_principal_basis(holding)
            key = (
                str(holding.get("market") or "").upper(),
                str(holding.get("symbol") or "").upper(),
            )
            update = updates.get(key)
            if not isinstance(update, dict):
                updated_holdings.append(enriched)
                continue
            enriched["dividend_source"] = str(update.get("source") or "內建瀏覽器即時網路搜尋")
            enriched["dividend_source_url"] = str(update.get("source_url") or "")
            enriched["dividend_updated_at"] = str(update.get("updated_at") or "")
            enriched["dividend_status"] = str(update.get("status") or "")
            current_name = str(enriched.get("name") or "").strip()
            online_name = str(update.get("name") or "").strip()
            if online_name and (
                not current_name or current_name.upper() == key[1].upper()
            ):
                enriched["name"] = online_name
                enriched["name_source"] = str(update.get("source") or "內建瀏覽器即時網路搜尋")
            instrument_type = str(update.get("instrument_type") or "").upper()
            asset_type_map = {
                "EQUITY": "STOCK",
                "ETF": "ETF",
                "MUTUALFUND": "FUND",
            }
            if str(enriched.get("asset_type") or "").upper() in {"", "AUTO"}:
                enriched["asset_type"] = asset_type_map.get(
                    instrument_type,
                    enriched.get("asset_type") or "AUTO",
                )
            online_currency = str(update.get("currency") or "").upper()
            if online_currency and not str(enriched.get("currency") or "").strip():
                enriched["currency"] = online_currency
            enriched["market_data_source"] = str(update.get("source") or "內建瀏覽器即時網路搜尋")
            if update.get("official_code"):
                enriched["fund_quote_symbol"] = str(update["official_code"])
                enriched["fund_identity_status"] = "confirmed"
            enriched["market_data_source_url"] = str(update.get("source_url") or "")
            enriched["market_data_updated_at"] = str(update.get("updated_at") or "")
            enriched["exchange_name"] = str(update.get("exchange_name") or "")
            if self._should_apply_synced_dividend_frequency(enriched, update):
                enriched["dividend_frequency"] = str(
                    update.get("dividend_frequency") or "unknown"
                )
                enriched["dividend_frequency_label"] = str(
                    update.get("dividend_frequency_label") or "待累積資料"
                )
                enriched["dividend_frequency_per_year"] = update.get(
                    "dividend_frequency_per_year"
                )
                enriched["dividend_frequency_median_days"] = update.get(
                    "dividend_frequency_median_days"
                )
                enriched["dividend_frequency_confidence"] = update.get(
                    "dividend_frequency_confidence"
                )
                enriched["dividend_frequency_source"] = "star-sync"
            enriched["external_annual_dividend_per_unit"] = update.get(
                "trailing_annual_dividend_per_unit"
            )
            if update.get("annual_dividend_yield_percent") is not None:
                enriched["annual_dividend_yield_percent"] = update[
                    "annual_dividend_yield_percent"
                ]
            annual_per_unit = number(
                update.get("trailing_annual_dividend_per_unit"), 0
            )
            annual_native = annual_per_unit * number(holding.get("quantity"), 0)
            source_currency = str(
                update.get("currency") or holding.get("currency") or "TWD"
            ).upper()
            fx_quote = self.v3.fx_rate(source_currency, "TWD")
            current_price = number(update.get("current_price"), 0)
            if current_price > 0:
                enriched["web_current_price"] = current_price
                enriched["web_current_price_currency"] = source_currency
                if fx_quote and number(fx_quote.get("rate"), 0) > 0:
                    enriched["web_current_value_twd"] = round(
                        current_price
                        * number(holding.get("quantity"), 0)
                        * number(fx_quote.get("rate")),
                        4,
                    )
            if annual_native > 0 and fx_quote and number(fx_quote.get("rate"), 0) > 0:
                annual_twd = annual_native * number(fx_quote["rate"])
                enriched["estimated_annual_dividend_twd"] = round(annual_twd, 4)
                enriched["estimated_weekly_dividend_twd"] = round(
                    annual_twd / 52.0, 4
                )
                enriched["monthly_dividend_twd"] = round(annual_twd / 12.0, 4)
                enriched["dividend_fx_provider"] = str(
                    fx_quote.get("provider") or ""
                )
                enriched["dividend_fx_rate"] = number(fx_quote.get("rate"))
            for event in update.get("events", []):
                if not isinstance(event, dict):
                    continue
                event_payloads.append(
                    {
                        "event_type": "dividend",
                        "symbol": key[1],
                        "title": f"{key[1]} 內建瀏覽器配息搜尋",
                        "scheduled_at": event.get("occurred_at"),
                        "source": update.get("source") or "Yahoo Finance",
                        "source_url": event.get("source_url") or update.get("source_url") or "",
                        "confidence": 0.85,
                        "details": {
                            "amount": event.get("amount_per_unit"),
                            "currency": source_currency,
                        },
                        "dedupe_key": (
                            f"online-dividend|{key[0]}|{key[1]}|"
                            f"{event.get('occurred_at')}"
                        ),
                    }
                )
            updated_holdings.append(enriched)
        if event_payloads:
            await asyncio.to_thread(
                self.analytics_store.add_events,
                event_payloads,
            )
        event_count = len(event_payloads)
        dividend_sync = {
            "provider": dividend_result.get("provider"),
            "updated_at": local_device_now().isoformat(),
            "portfolio_imported_at": str(
                (state.get("portfolio") or {}).get("imported_at") or ""
            ),
            "portfolio_manual_revision": int(
                (state.get("portfolio") or {}).get("manual_revision") or 0
            ),
            "requested_count": dividend_result.get("requested_count", 0),
            "updated_count": dividend_result.get("updated_count", 0),
            "error_count": dividend_result.get("error_count", 0),
            "fx_provider": fx_result.get("fx_provider"),
            "fx_observed_at": fx_result.get("fx_observed_at"),
            "weekly_standard": True,
            "display_currency": "TWD",
        }
        await asyncio.to_thread(
            self._merge_synchronized_holdings,
            holdings,
            updated_holdings,
            sync_metadata={"dividend_sync": dividend_sync},
            sync_owned_fields={
                "dividend_source",
                "dividend_source_url",
                "dividend_updated_at",
                "dividend_status",
                "dividend_frequency",
                "dividend_frequency_label",
                "dividend_frequency_per_year",
                "dividend_frequency_median_days",
                "dividend_frequency_confidence",
                "external_annual_dividend_per_unit",
                "annual_dividend_yield_percent",
                "estimated_annual_dividend_twd",
                "estimated_weekly_dividend_twd",
                "monthly_dividend_twd",
                "dividend_fx_provider",
                "dividend_fx_rate",
                "web_current_price",
                "web_current_price_currency",
                "web_current_value_twd",
                "market_data_source",
                "market_data_source_url",
                "market_data_updated_at",
                "exchange_name",
                "fund_quote_symbol",
                "fund_identity_status",
            },
        )
        await asyncio.to_thread(
            self.analytics_store.audit,
            "online_dividend_sync",
            {
                **dividend_sync,
                "event_count": event_count,
                "errors": dividend_result.get("errors", [])[:20],
            },
            severity=(
                "warning" if number(dividend_result.get("error_count")) > 0 else "info"
            ),
        )
        self._invalidate_v3_snapshot()
        latest_state = await asyncio.to_thread(self.repository.load_state)
        response = await asyncio.to_thread(self._state_response, latest_state)
        response.update(
            {
                "message": (
                    f"內建瀏覽器配息搜尋已更新 {dividend_result.get('updated_count', 0)} 筆；"
                    "週配息已依華南銀行匯率換算為新台幣。"
                ),
                "dividend_sync": dividend_result,
                "fx_sync": fx_result,
            }
        )
        return response
