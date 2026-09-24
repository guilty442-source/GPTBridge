"""共同基金 domain — business-layer read surface over fund mirrors.

The fund engine (investment-mobile) owns NAV/distribution/transaction/
recommendation computation. This domain exposes the authoritative
business copy mirrored through the governed bridge — read-only for
星澄 queries; it never mutates balances or fund units.
"""

from __future__ import annotations

from ...domain.contract import DOMAIN_FUND
from .base import BusinessDomain


class FundDomain(BusinessDomain):
    domain_id = DOMAIN_FUND
    label = "共同基金"
    market = "fund"
    commands = frozenset(
        {
            "investment_fund_portfolio",
            "investment_fund_recommendations",
            "investment_fund_platforms",
            "investment_fund_nav",
            "investment_fund_transactions",
            "investment_fund_analysis",
        }
    )

    async def handle(self, command, payload, *, store, ai_connections):
        if command == "investment_fund_portfolio":
            return {
                "ok": True,
                "domain": self.domain_id,
                "positions": store.positions(market=self.market),
                "recent_transactions": store.fund_transactions(limit=20),
            }
        if command == "investment_fund_recommendations":
            # 申購/贖回建議 — mirrored engine records + 星澄候選訊號
            return {
                "ok": True,
                "domain": self.domain_id,
                "recommendations": store.fund_recommendations(
                    fund_id=str(payload.get("fund_id") or "") or None,
                    limit=int(payload.get("limit") or 50),
                ),
                "candidate_signals": store.signals(market=self.market, limit=50),
                "note": "建議僅供參考；基金實盤交易本階段停用",
            }
        if command == "investment_fund_platforms":
            platforms = store.kv_get(self.domain_id, "platforms", [])
            return {
                "ok": True,
                "domain": self.domain_id,
                "platforms": platforms,
                "extensible": True,
                "note": "多平台整合預留；接入前需平台 API 驗證",
            }
        if command == "investment_fund_nav":
            fund_id = str(payload.get("fund_id") or "")
            if not fund_id:
                return {"ok": False, "error_code": "FUND_ID_REQUIRED"}
            return {
                "ok": True,
                "domain": self.domain_id,
                "navs": store.fund_navs(
                    fund_id,
                    share_class_id=str(payload.get("share_class_id") or "") or None,
                    limit=int(payload.get("limit") or 500),
                ),
                "note": "淨值為公告值，非即時成交價",
            }
        if command == "investment_fund_transactions":
            return {
                "ok": True,
                "domain": self.domain_id,
                "transactions": store.fund_transactions(
                    account_id=str(payload.get("account_id") or "") or None,
                    limit=int(payload.get("limit") or 200),
                ),
            }
        if command == "investment_fund_analysis":
            # 星澄 analysis request — engine-side data is authoritative;
            # this returns the mirrored basis + provenance markers.
            fund_id = str(payload.get("fund_id") or "")
            return {
                "ok": True,
                "domain": self.domain_id,
                "fund_id": fund_id,
                "navs": store.fund_navs(fund_id, limit=500) if fund_id else [],
                "recommendations": store.fund_recommendations(
                    fund_id=fund_id or None),
                "answer_kinds": ["verified_fact", "computed_result",
                                 "model_analysis", "unconfirmed"],
                "note": "分析區分已驗證事實/計算結果/模型分析/未確認資訊",
            }
        raise PermissionError("PERMISSION_DENIED")
