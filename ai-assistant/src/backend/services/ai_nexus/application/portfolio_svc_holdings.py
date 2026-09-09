from __future__ import annotations

from typing import Any

from ..infrastructure.analytics_repository import number


class PortfolioSvcHoldingsMixin:
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
