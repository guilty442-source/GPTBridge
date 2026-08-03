from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any

from ..infrastructure.analytics_repository import market_session_status, number


def local_device_now() -> datetime:
    return datetime.now().astimezone()


class InvestmentPortfolioServiceMixin:
    async def _get_analytics(self, _payload: dict[str, Any]) -> dict[str, Any]:
        return self._state_response(self.repository.load_state())

    async def _add_transaction(self, payload: dict[str, Any]) -> dict[str, Any]:
        transaction = self.analytics_store.add_transaction(payload)
        accounting = self._schedule_star_accounting_background(
            trigger="transaction_change"
        )
        response = self._state_response(self.repository.load_state())
        response.update(
            {
                "message": "交易已寫入本機帳本，星澄帳務已接續排程。",
                "transaction": transaction,
                "star_accounting": accounting,
            }
        )
        return response

    async def _seed_opening_ledger(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._snapshot_coordinator() as state:
            portfolio = (
                state.get("portfolio")
                if isinstance(state.get("portfolio"), dict)
                else {}
            )
            occurred_at = str(payload.get("occurred_at") or "").strip()
            if not occurred_at:
                source_path = Path(str(portfolio.get("source_path") or ""))
                try:
                    occurred_at = datetime.fromtimestamp(
                        source_path.stat().st_ctime
                    ).astimezone().isoformat()
                except OSError:
                    occurred_at = str(portfolio.get("imported_at") or "")
            safety_backup = self.analytics_store.backup_database(
                "before-opening-ledger"
            )
            result = self.analytics_store.sync_opening_balance_transactions(
                state,
                occurred_at,
            )
            self._invalidate_v3_snapshot()
            response = self._state_response(state)
        accounting = self._schedule_star_accounting_background(
            trigger="opening_ledger_change"
        )
        response.update(
            {
                "message": (
                    f"已建立 {result.get('generated_count', 0)} 筆估算期初交易；"
                    f"覆蓋 {result.get('coverage_percent', 0)}%，XIRR 已改用新台幣基準。"
                ),
                "opening_ledger": result,
                "safety_backup": safety_backup,
                "star_accounting": accounting,
            }
        )
        return response

    def _enrich_holding_principal_basis(
        self,
        holding: dict[str, Any],
    ) -> dict[str, Any]:
        enriched = dict(holding)
        quantity = number(holding.get("quantity"), 0)
        principal_amount = number(
            holding.get("principal_amount"),
            number(holding.get("average_cost"), 0) * quantity,
        )
        principal_currency = str(
            holding.get("principal_currency")
            or holding.get("currency")
            or "TWD"
        ).upper()
        principal_fx = self.v3.fx_rate(principal_currency, "TWD")
        if (
            principal_amount > 0
            and principal_fx
            and number(principal_fx.get("rate"), 0) > 0
        ):
            enriched["principal_amount"] = principal_amount
            enriched["principal_currency"] = principal_currency
            enriched["principal_twd"] = round(
                principal_amount * number(principal_fx.get("rate")),
                4,
            )
            enriched["principal_fx_provider"] = str(
                principal_fx.get("provider") or ""
            )
            enriched["principal_fx_rate"] = number(principal_fx.get("rate"))
        asset_currency = str(holding.get("currency") or "TWD").upper()
        asset_fx = self.v3.fx_rate(asset_currency, "TWD")
        principal_twd = number(enriched.get("principal_twd"), 0)
        if (
            asset_currency != principal_currency
            and quantity > 0
            and principal_twd > 0
            and asset_fx
            and number(asset_fx.get("rate"), 0) > 0
        ):
            enriched["average_cost"] = round(
                principal_twd / number(asset_fx.get("rate")) / quantity,
                8,
            )
            enriched["average_cost_currency"] = asset_currency
            enriched["average_cost_method"] = (
                "principal_twd_huanan_current_fx_estimate"
            )
            enriched["average_cost_fx_rate"] = number(asset_fx.get("rate"))
        return enriched

    @staticmethod
    def _normalized_manual_holding(
        payload: dict[str, Any],
        existing: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        symbol = str(payload.get("symbol") or "").strip().upper()
        market = str(payload.get("market") or "").strip().upper()
        asset_type = str(payload.get("asset_type") or "STOCK").strip().upper()
        currency = str(payload.get("currency") or "TWD").strip().upper()
        quantity = number(payload.get("quantity"), -1)
        average_cost = number(payload.get("average_cost"), -1)
        def optional_number(field: str) -> float | None:
            raw_value = payload.get(field, (existing or {}).get(field))
            if raw_value is None or raw_value == "":
                return None
            parsed = number(raw_value, -1)
            if parsed < 0:
                raise ValueError(f"{field} 不可小於零")
            return parsed

        dividend_amount_twd = optional_number("dividend_amount_twd")
        dividend_per_unit = optional_number("dividend_per_unit")
        monthly_dividend_twd = optional_number("monthly_dividend_twd")
        annual_dividend_yield_percent = optional_number(
            "annual_dividend_yield_percent"
        )
        payback_rate_percent = optional_number("payback_rate_percent")
        current_value_twd = optional_number("current_value_twd")
        principal_amount = optional_number("principal_amount")
        principal_twd = optional_number("principal_twd")
        principal_currency = str(
            payload.get("principal_currency")
            or (existing or {}).get("principal_currency")
            or currency
        ).strip().upper()
        frequency_definitions = {
            "unknown": ("待確認", None),
            "none": ("無配息", 0),
            "weekly": ("每週", 52),
            "biweekly": ("每兩週", 26),
            "monthly": ("每月", 12),
            "bimonthly": ("每兩月", 6),
            "quarterly": ("每季", 4),
            "semiannual": ("每半年", 2),
            "annual": ("每年", 1),
            "irregular": ("不定期", None),
        }
        dividend_frequency = str(
            payload.get(
                "dividend_frequency",
                (existing or {}).get("dividend_frequency") or "unknown",
            )
            or "unknown"
        ).strip().casefold()
        if dividend_frequency not in frequency_definitions:
            raise ValueError("配息頻率不在允許清單")
        dividend_frequency_label, dividend_frequency_per_year = (
            frequency_definitions[dividend_frequency]
        )
        fund_code = str(
            payload.get("fund_code") or (existing or {}).get("fund_code") or ""
        ).strip().upper()
        fund_isin = str(
            payload.get("fund_isin") or (existing or {}).get("fund_isin") or ""
        ).strip().upper()
        fund_share_class = str(
            payload.get("fund_share_class")
            or (existing or {}).get("fund_share_class")
            or ""
        ).strip()
        fund_quote_symbol = str(
            payload.get("fund_quote_symbol")
            or (existing or {}).get("fund_quote_symbol")
            or ""
        ).strip().upper()
        estimated_annual_dividend_twd = (
            0.0
            if dividend_frequency == "none"
            else monthly_dividend_twd * 12.0
            if monthly_dividend_twd is not None and monthly_dividend_twd > 0
            else dividend_per_unit * quantity * dividend_frequency_per_year
            if dividend_per_unit is not None
            and dividend_per_unit > 0
            and dividend_frequency_per_year is not None
            and dividend_frequency_per_year > 0
            else current_value_twd * annual_dividend_yield_percent / 100.0
            if current_value_twd is not None
            and annual_dividend_yield_percent is not None
            else None
        )
        estimated_weekly_dividend_twd = (
            estimated_annual_dividend_twd / 52.0
            if estimated_annual_dividend_twd is not None
            else None
        )
        if not symbol:
            raise ValueError("持股代號不可空白")
        if quantity < 0:
            raise ValueError("持股數量不可小於零")
        if average_cost < 0:
            raise ValueError("平均成本不可小於零")
        if principal_amount is None and average_cost > 0 and quantity > 0:
            principal_amount = average_cost * quantity
        if principal_twd is None and principal_currency == "TWD":
            principal_twd = principal_amount
        if not market:
            raise ValueError("請選擇市場")
        if not currency or len(currency) > 8 or not currency.replace("-", "").isalnum():
            raise ValueError("幣別格式不正確")
        if (
            not principal_currency
            or len(principal_currency) > 8
            or not principal_currency.replace("-", "").isalnum()
        ):
            raise ValueError("本金幣別格式不正確")
        return {
            **(existing or {}),
            "holding_id": str((existing or {}).get("holding_id") or payload.get("holding_id") or ""),
            "symbol": symbol,
            "name": str(payload.get("name") or symbol).strip(),
            "market": market,
            "asset_type": asset_type,
            "quantity": quantity,
            "average_cost": average_cost,
            "currency": currency,
            "principal_amount": principal_amount,
            "principal_currency": principal_currency,
            "principal_twd": principal_twd,
            "fund_code": fund_code,
            "fund_isin": fund_isin,
            "fund_share_class": fund_share_class,
            "fund_quote_symbol": fund_quote_symbol,
            "fund_identity_status": (
                "confirmed"
                if fund_quote_symbol
                else str((existing or {}).get("fund_identity_status") or "")
            ),
            "source_row": (existing or {}).get("source_row"),
            "dividend_amount_twd": dividend_amount_twd,
            "dividend_per_unit": dividend_per_unit,
            "monthly_dividend_twd": monthly_dividend_twd,
            "annual_dividend_yield_percent": annual_dividend_yield_percent,
            "dividend_frequency": dividend_frequency,
            "dividend_frequency_label": dividend_frequency_label,
            "dividend_frequency_per_year": dividend_frequency_per_year,
            "dividend_frequency_source": "manual",
            "dividend_frequency_confidence": 1.0,
            "payback_rate_percent": payback_rate_percent,
            "current_value_twd": current_value_twd,
            "estimated_annual_dividend_twd": estimated_annual_dividend_twd,
            "estimated_weekly_dividend_twd": estimated_weekly_dividend_twd,
            "manually_edited": True,
        }

    @staticmethod
    def _should_apply_synced_dividend_frequency(
        holding: dict[str, Any],
        update: dict[str, Any],
    ) -> bool:
        current_source = str(
            holding.get("dividend_frequency_source") or ""
        ).strip().casefold()
        synced_frequency = str(
            update.get("dividend_frequency") or ""
        ).strip().casefold()
        if current_source != "manual":
            return True
        return synced_frequency not in {"", "unknown"}

    async def _upsert_holding(self, payload: dict[str, Any]) -> dict[str, Any]:
        state = self.repository.load_state()
        holdings = [dict(item) for item in state.get("holdings", []) if isinstance(item, dict)]
        holding_id = str(payload.get("holding_id") or "").strip()
        target_index = next(
            (index for index, item in enumerate(holdings) if str(item.get("holding_id") or "") == holding_id),
            None,
        ) if holding_id else None
        if holding_id and target_index is None:
            raise ValueError("找不到要修改的持股")
        existing = holdings[target_index] if target_index is not None else None
        normalized = self._normalized_manual_holding(payload, existing)
        duplicate = next(
            (
                item
                for index, item in enumerate(holdings)
                if index != target_index
                and str(item.get("symbol") or "").upper() == normalized["symbol"]
                and str(item.get("market") or "").upper() == normalized["market"]
            ),
            None,
        )
        if duplicate:
            raise ValueError("同一市場已有相同持股代號")
        action = "update" if target_index is not None else "create"
        if target_index is None:
            holdings.append(normalized)
        else:
            holdings[target_index] = normalized
        saved = self.repository.replace_holdings(
            holdings,
            change={"action": action, "symbol": normalized["symbol"]},
        )
        saved_holding = next(
            (
                item
                for item in saved.get("holdings", [])
                if item.get("symbol") == normalized["symbol"] and item.get("market") == normalized["market"]
            ),
            normalized,
        )
        self.analytics_store.audit(
            "holding_manually_updated",
            {
                "action": action,
                "holding_id": saved_holding.get("holding_id"),
                "before": existing,
                "after": saved_holding,
                "source_file_modified": False,
            },
            severity="warning",
        )
        self._invalidate_v3_snapshot()
        if payload.get("refresh_quotes", True) and saved.get("holdings"):
            self._schedule_local_risk_ai_background(
                saved,
                {"trigger": "manual_holding_change", "live_quotes": True},
            )
        response = self._state_response(self.repository.load_state())
        response.update(
            {
                "message": "持股已更新；原始 Excel 未被修改。" if action == "update" else "持股已新增；資料只保存於本機。",
                "holding": saved_holding,
                "source_file_modified": False,
            }
        )
        return response

    async def _delete_holding(self, payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("confirmed") is not True:
            raise ValueError("刪除持股前必須明確確認")
        holding_id = str(payload.get("holding_id") or "").strip()
        if not holding_id:
            raise ValueError("缺少 holding_id")
        state = self.repository.load_state()
        holdings = [dict(item) for item in state.get("holdings", []) if isinstance(item, dict)]
        removed = next((item for item in holdings if str(item.get("holding_id") or "") == holding_id), None)
        if not removed:
            raise ValueError("找不到要刪除的持股")
        remaining = [item for item in holdings if str(item.get("holding_id") or "") != holding_id]
        self.repository.replace_holdings(
            remaining,
            change={"action": "delete", "symbol": removed.get("symbol")},
        )
        self.analytics_store.audit(
            "holding_manually_deleted",
            {"holding": removed, "source_file_modified": False},
            severity="warning",
        )
        self._invalidate_v3_snapshot()
        response = self._state_response(self.repository.load_state())
        response.update(
            {
                "message": "持股已從本機清單刪除；原始 Excel 未被修改。",
                "deleted": True,
                "source_file_modified": False,
            }
        )
        return response

    async def _restore_portfolio_version(self, payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("confirmed") is not True:
            raise ValueError("還原持股版本前必須明確確認")
        version_id = str(payload.get("version_id") or "").strip()
        restored = self.repository.restore_state_version(version_id)
        self.analytics_store.audit(
            "portfolio_version_restored",
            {"version_id": version_id, "holding_count": len(restored.get("holdings", []))},
            severity="warning",
        )
        self._invalidate_v3_snapshot()
        if restored.get("holdings"):
            self._schedule_local_risk_ai_background(
                restored,
                {"trigger": "portfolio_version_restore", "live_quotes": True},
            )
        response = self._state_response(self.repository.load_state())
        response["message"] = f"已還原持股版本 {version_id}，並保留還原前版本。"
        return response

    async def _resolve_fund_identities(self, payload: dict[str, Any]) -> dict[str, Any]:
        state = self.repository.load_state()
        holdings = [dict(item) for item in state.get("holdings", []) if isinstance(item, dict)]
        if self.ai_connections is None:
            raise ValueError("星澄 AI 通道尚未連線，基金辨識未執行且不排隊。")
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
            "provider": "local-ai",
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

    async def _reconcile_ledger(self, _payload: dict[str, Any]) -> dict[str, Any]:
        with self._snapshot_coordinator() as state:
            reconciliation = self.analytics_store.reconcile_ledger_holdings(state)
            response = self._state_response(state)
        response.update(
            {
                "message": (
                    f"帳本對帳完成：{reconciliation.get('matched_count', 0)} 筆相符，"
                    f"{reconciliation.get('difference_count', 0)} 筆差異。"
                ),
                "ledger_reconciliation": reconciliation,
            }
        )
        return response

    async def _apply_ledger_reconciliation(self, payload: dict[str, Any]) -> dict[str, Any]:
        with self._snapshot_coordinator() as state:
            safety_backup = self.analytics_store.backup_database(
                "before-ledger-reconciliation"
            )
            reconciliation = self.analytics_store.apply_ledger_reconciliation(
                state,
                confirmed=payload.get("confirmed") is True,
            )
            self._invalidate_v3_snapshot()
            response = self._state_response(state)
        response.update(
            {
                "message": (
                    f"已建立 {reconciliation.get('applied_count', 0)} 筆估算對帳調整；"
                    "原始持股與券商檔案均未修改。"
                ),
                "ledger_reconciliation": reconciliation,
                "safety_backup": safety_backup,
            }
        )
        return response

    async def _delete_transaction(self, payload: dict[str, Any]) -> dict[str, Any]:
        transaction_id = str(payload.get("transaction_id") or "").strip()
        if not transaction_id:
            raise ValueError("缺少 transaction_id")
        deleted = self.analytics_store.delete_transaction(transaction_id)
        response = self._state_response(self.repository.load_state())
        response.update({"message": "交易已刪除。" if deleted else "找不到交易。", "deleted": deleted})
        return response

    async def _import_price_history(self, payload: dict[str, Any]) -> dict[str, Any]:
        raw_bars = payload.get("bars")
        if not isinstance(raw_bars, list):
            raise ValueError("bars 必須是歷史行情陣列")
        count = self.analytics_store.add_price_bars(
            item for item in raw_bars if isinstance(item, dict)
        )
        response = self._state_response(self.repository.load_state())
        response.update({"message": f"已匯入 {count} 筆歷史行情。", "price_count": count})
        return response

    async def _sync_intelligence(self, payload: dict[str, Any]) -> dict[str, Any]:
        state = self.repository.load_state()
        holdings = [item for item in state.get("holdings", []) if isinstance(item, dict)]
        if not holdings:
            raise ValueError("請先讀取持股檔，再同步市場情報")
        del payload
        if self.ai_connections is None:
            raise ValueError("星澄 AI 通道尚未連線，市場情報未執行且不排隊。")
        star_result = await asyncio.to_thread(
            self.ai_connections.search_investments_sync, holdings
        )
        if star_result.get("ok") is not True:
            raise ValueError(str(star_result.get("message") or "星澄市場情報服務失敗。"))
        result = {
            "provider": "local-ai",
            "service_owner": "星澄",
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

    @staticmethod
    def _holding_merge_key(holding: dict[str, Any]) -> str:
        holding_id = str(holding.get("holding_id") or "").strip()
        if holding_id:
            return f"id:{holding_id}"
        return (
            f"symbol:{str(holding.get('market') or '').strip().upper()}|"
            f"{str(holding.get('symbol') or '').strip().upper()}"
        )

    def _merge_synchronized_holdings(
        self,
        original_holdings: list[dict[str, Any]],
        synchronized_holdings: list[dict[str, Any]],
        *,
        sync_metadata: dict[str, Any],
        sync_owned_fields: set[str],
    ) -> dict[str, Any]:
        """Three-way merge network enrichment without reverting manual edits."""

        original_by_key = {
            self._holding_merge_key(item): item for item in original_holdings
        }
        synchronized_by_key = {
            self._holding_merge_key(item): item for item in synchronized_holdings
        }

        def mutate(latest_state: dict[str, Any]) -> None:
            merged: list[dict[str, Any]] = []
            for current_item in latest_state.get("holdings", []):
                if not isinstance(current_item, dict):
                    continue
                key = self._holding_merge_key(current_item)
                original = original_by_key.get(key)
                synchronized = synchronized_by_key.get(key)
                if original is None or synchronized is None:
                    # Newly added holdings remain untouched; deleted holdings are
                    # absent from latest_state and therefore cannot be resurrected.
                    merged.append(dict(current_item))
                    continue
                current = dict(current_item)
                for field, value in synchronized.items():
                    if field == "holding_id" or value == original.get(field):
                        continue
                    if field in sync_owned_fields or current.get(field) == original.get(field):
                        current[field] = value
                merged.append(current)
            latest_state["holdings"] = merged
            latest_state.update(sync_metadata)

        return self.repository.update_state(mutate)

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
                    str(star_result.get("message") or "星澄尚未連線，報價未送出且不排隊。")
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
                "provider": "星澄即時網路搜尋",
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
                "methodology": "星澄搜尋具來源與日期的市價或基金淨值",
                "limitations": ["共同基金淨值不是盤中成交價。", "無法驗證的結果不寫入。"],
            }
        else:
            raise ValueError("星澄 AI 通道尚未連線，報價未執行且不排隊。")
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
                        result.get("provider") or "星澄即時網路搜尋"
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
                    f"星澄報價已更新 "
                    f"{result.get('updated_count', 0)} 筆。"
                ),
                "market_sessions": sessions,
                "market_quote_sync": result,
            }
        )
        return response

    async def _sync_dividends(self, _payload: dict[str, Any]) -> dict[str, Any]:
        state = self.repository.load_state()
        holdings = [
            dict(item) for item in state.get("holdings", []) if isinstance(item, dict)
        ]
        if not holdings:
            raise ValueError("請先讀取持股檔，再由星澄搜尋配息")
        currencies = [
            str(currency or "")
            for item in holdings
            for currency in (
                item.get("currency"),
                item.get("principal_currency"),
            )
        ]
        fx_result = {
            "fx_provider": "local-ai",
            "fx_service_owner": "星澄",
            "fx_requested_currencies": sorted(set(currencies)),
        }
        if self.ai_connections is not None:
            star_result = await asyncio.to_thread(
                self.ai_connections.search_investments_sync,
                [item for item in holdings if number(item.get("quantity"), 0) > 0],
            )
            if not star_result.get("results"):
                raise ValueError(
                    str(star_result.get("message") or "星澄尚未連線，配息搜尋未送出且不排隊。")
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
                        "source": "星澄即時網路搜尋",
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
                "provider": "星澄即時網路搜尋",
                "updated_at": star_result.get("searched_at"),
                "coverage_percent": round(len(star_updates) / max(1, int(star_result.get("requested_count") or 0)) * 100, 2),
                "methodology": "星澄搜尋公開市場配息事件與官方基金配息公告",
                "limitations": ["基金公告未揭露可驗證金額時只保存公告與頻率，不推造配息金額。"],
            }
        else:
            raise ValueError("星澄 AI 通道尚未連線，配息搜尋未執行且不排隊。")
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
            enriched["dividend_source"] = str(update.get("source") or "星澄即時網路搜尋")
            enriched["dividend_source_url"] = str(update.get("source_url") or "")
            enriched["dividend_updated_at"] = str(update.get("updated_at") or "")
            enriched["dividend_status"] = str(update.get("status") or "")
            current_name = str(enriched.get("name") or "").strip()
            online_name = str(update.get("name") or "").strip()
            if online_name and (
                not current_name or current_name.upper() == key[1].upper()
            ):
                enriched["name"] = online_name
                enriched["name_source"] = str(update.get("source") or "星澄即時網路搜尋")
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
            enriched["market_data_source"] = str(update.get("source") or "星澄即時網路搜尋")
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
                        "title": f"{key[1]} 星澄配息搜尋",
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
                    f"星澄配息搜尋已更新 {dividend_result.get('updated_count', 0)} 筆；"
                    "週配息已依華南銀行匯率換算為新台幣。"
                ),
                "dividend_sync": dividend_result,
                "fx_sync": fx_result,
            }
        )
        return response
