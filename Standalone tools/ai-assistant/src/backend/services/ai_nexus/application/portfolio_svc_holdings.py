from __future__ import annotations

from typing import Any

from ..infrastructure.analytics_repository import number
from .portfolio_svc_holdings_manual import PortfolioSvcHoldingsManualMixin


class PortfolioSvcHoldingsMixin(PortfolioSvcHoldingsManualMixin):
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
        self._enrich_principal_fx(enriched, principal_amount, principal_currency)
        self._enrich_average_cost_fx(
            enriched, holding, quantity, principal_currency
        )
        return enriched

    def _enrich_principal_fx(
        self,
        enriched: dict[str, Any],
        principal_amount: float,
        principal_currency: str,
    ) -> None:
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

    def _enrich_average_cost_fx(
        self,
        enriched: dict[str, Any],
        holding: dict[str, Any],
        quantity: float,
        principal_currency: str,
    ) -> None:
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
        self._assert_no_duplicate_holding(holdings, target_index, normalized)
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
        self._audit_holding_update(action, saved_holding, existing)
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

    def _assert_no_duplicate_holding(
        self,
        holdings: list[dict[str, Any]],
        target_index: int | None,
        normalized: dict[str, Any],
    ) -> None:
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

    def _audit_holding_update(
        self,
        action: str,
        saved_holding: dict[str, Any],
        existing: dict[str, Any] | None,
    ) -> None:
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
