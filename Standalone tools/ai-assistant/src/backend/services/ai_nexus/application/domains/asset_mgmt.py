"""全資產管理 domain — cross-market aggregation."""

from __future__ import annotations

from collections import defaultdict

from ...domain.contract import DOMAIN_ASSET_MGMT
from .base import BusinessDomain


class AssetManagementDomain(BusinessDomain):
    domain_id = DOMAIN_ASSET_MGMT
    label = "全資產管理"
    commands = frozenset(
        {
            "investment_assets_summary",
            "investment_assets_holdings",
            "investment_assets_transactions",
            "investment_assets_valuation",
            "investment_assets_allocation",
            "investment_assets_exposure",
            # 星澄監測中心 mirrors (read-only, advisory only)
            "investment_monitor_overview",
            "investment_monitor_events",
            "investment_monitor_recommendations",
            "investment_monitor_opportunities",
            "investment_monitor_risk",
            "investment_monitor_reports",
            "investment_monitor_notifications",
            # 星澄自動模擬操盤 mirrors (read-only, simulated only)
            "investment_autotrade_overview",
            "investment_autotrade_strategies",
            "investment_autotrade_performance",
            "investment_autotrade_reports",
            # Display-only settings — trading/risk limits are never
            # settable through this surface
            "investment_settings_get",
            "investment_settings_set",
        }
    )

    async def handle(self, command, payload, *, store, ai_connections):
        if command == "investment_assets_summary":
            positions = store.positions()
            by_market: dict[str, float] = defaultdict(float)
            for position in positions:
                by_market[position["market"]] += (
                    position["quantity"] * position.get("average_cost", 0.0)
                )
            return {
                "ok": True,
                "domain": self.domain_id,
                "markets": dict(by_market),
                "total_cost_basis": sum(by_market.values()),
                "position_count": len(positions),
            }
        if command == "investment_assets_holdings":
            return {
                "ok": True,
                "domain": self.domain_id,
                "holdings": store.positions(),
            }
        if command == "investment_assets_transactions":
            return {
                "ok": True,
                "domain": self.domain_id,
                "orders": store.orders(limit=int(payload.get("limit") or 200)),
                "executions": store.executions(limit=int(payload.get("limit") or 200)),
            }
        # Offline asset-center mirrors pushed by investment-mobile —
        # read-only views; the authoritative books live in the offline
        # journal (broker_confirmed=false forever this phase).
        if command == "investment_assets_valuation":
            return {
                "ok": True,
                "domain": self.domain_id,
                "valuation": store.kv_get(
                    "asset-management", "asset_value", {}),
                "source": "investment-mobile mirror",
                "broker_confirmed": False,
            }
        if command == "investment_assets_allocation":
            return {
                "ok": True,
                "domain": self.domain_id,
                "allocation": store.kv_get(
                    "asset-management", "asset_allocation_analyze", {}),
                "source": "investment-mobile mirror",
            }
        if command == "investment_assets_exposure":
            return {
                "ok": True,
                "domain": self.domain_id,
                "exposure": store.kv_get(
                    "asset-management", "asset_exposure_analyze", {}),
                "source": "investment-mobile mirror",
            }
        # 監測中心鏡像 — advisory projections only; nothing here can
        # trade or mutate authority. Keys mirror the push side's
        # `monitor_<suffix>` naming.
        monitoring_views = {
            "investment_monitor_overview": "monitor_overview",
            "investment_monitor_events": "monitor_events",
            "investment_monitor_recommendations": "monitor_rec-list",
            "investment_monitor_opportunities":
                "monitor_scan-opportunities",
            "investment_monitor_risk": "monitor_risk-check",
            "investment_monitor_reports": "monitor_report-list",
            "investment_monitor_notifications": "monitor_notifications",
        }
        if command in monitoring_views:
            return {
                "ok": True,
                "domain": self.domain_id,
                "view": store.kv_get(
                    "monitoring", monitoring_views[command], {}),
                "source": "investment-mobile mirror",
                "advisory": True,
                "note": "監測與建議僅供參考——非交易指令，"
                        "資料時間見各筆記錄",
            }
        # 自動模擬操盤鏡像 — SHADOW/PAPER simulated state only; never
        # authoritative, never a real-trade trigger.
        autotrade_views = {
            "investment_autotrade_overview": "at_overview",
            "investment_autotrade_strategies": "at_strategy-list",
            "investment_autotrade_performance": "at_performance",
            "investment_autotrade_reports": "at_report-list",
        }
        if command in autotrade_views:
            return {
                "ok": True,
                "domain": self.domain_id,
                "view": store.kv_get(
                    "autotrade", autotrade_views[command], {}),
                "source": "investment-mobile mirror",
                "simulated": True,
                "note": "SHADOW/PAPER 模擬紀錄——非真實帳戶收益，"
                        "不構成交易指令",
            }
        # 顯示偏好設定 — only whitelisted display keys are writable;
        # trading authorizations and risk limits are NEVER settable here.
        _SETTINGS_ALLOWED = frozenset({
            "display_currency", "theme", "font_scale",
            "sidebar_collapsed", "default_page", "watchlist_tw",
            "watchlist_us", "notification_mute",
        })
        if command == "investment_settings_get":
            return {
                "ok": True,
                "domain": self.domain_id,
                "settings": store.kv_get("settings", "display", {}),
                "writable_keys": sorted(_SETTINGS_ALLOWED),
                "note": "正式交易授權與風控上限不得由前端設定",
            }
        if command == "investment_settings_set":
            patch = dict(payload.get("settings") or {})
            denied = [k for k in patch if k not in _SETTINGS_ALLOWED]
            if denied:
                return {"ok": False,
                        "error_code": "SETTING_KEY_DENIED",
                        "denied": denied}
            cur = dict(store.kv_get("settings", "display", {}) or {})
            cur.update({k: patch[k] for k in patch})
            store.kv_set("settings", "display", cur)
            return {"ok": True, "settings": cur}
        raise PermissionError("PERMISSION_DENIED")
