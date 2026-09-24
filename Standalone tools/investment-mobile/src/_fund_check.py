"""Mutual-fund engine acceptance tests — 24 required cases."""
import sys
import tempfile
import unittest
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

TOOL_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOL_ROOT / "src" / "backend" / "services"))

from investment_mobile.trading.fund import (
    DistributionSource, FeeCalc, FeeKind, FundDistribution, FundFee,
    FundHolding, FundIdentity, FundNAV, FundRecommendation,
    FundTransaction, FundTransactionType, FundTxnStatus,
    MutualFundEngine, NavType, RecommendationType,
)
from investment_mobile.trading.fund.providers import (
    PROVIDER_CAPABILITIES, import_nav_rows,
)
from investment_mobile.trading.market.fx import CurrencyRateService

FUND = "F001"
CLS_A = "A"          # TWD accumulation
CLS_B = "B-USD"      # USD distribution
IID_A = f"fund:{FUND}:{CLS_A}:TWD"
IID_B = f"fund:{FUND}:{CLS_B}:USD"
ACCT = "fund-provider-generic"


def _engine(tmp):
    fx = CurrencyRateService(Path(tmp))
    return MutualFundEngine(Path(tmp), fx)


def _ident(engine, cls=CLS_A, dist="accumulation", ccy="TWD"):
    return engine.identities.register({
        "fund_id": FUND, "fund_name": "測試科技基金",
        "fund_company": "TestAM", "fund_type": "equity",
        "share_class_id": cls, "share_class_name": f"{cls}級別",
        "share_class_currency": ccy, "base_currency": "TWD",
        "isin": "TW000T0001A0", "domicile": "TW",
        "distribution_policy": dist, "inception_date": "2020-01-01",
    })


def _nav(fid, cls, day, nav, rev=1, ntype="published"):
    return FundNAV(fund_id=fid, share_class_id=cls,
                   nav_date=date(2026, 9, day), nav=nav, currency="TWD",
                   source_id="tdcc", nav_type=ntype, revision=rev)


class FundAcceptance(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = self._tmp.name
        self.eng = _engine(self.tmp)

    # 1 — fund body vs share class identity
    def test_fund_body_vs_share_class(self):
        _ident(self.eng, CLS_A)
        _ident(self.eng, CLS_B, dist="distribution", ccy="USD")
        a = self.eng.identities.get(IID_A)
        b = self.eng.identities.get(IID_B)
        self.assertEqual(a["fund_id"], b["fund_id"])          # same body
        self.assertNotEqual(a["instrument_id"], b["instrument_id"])
        ida = self.eng.identities.get(IID_A)
        self.assertEqual(ida["share_class_currency"], "TWD")
        classes = self.eng.identities.share_classes(FUND)
        self.assertEqual(len(classes), 2)

    # 2 — different currencies distinct
    def test_currency_distinction(self):
        _ident(self.eng, CLS_A)
        _ident(self.eng, "A-USD", ccy="USD")
        self.assertIsNone(self.eng.identities.get(
            f"fund:{FUND}:A-USD:TWD"))   # wrong currency → different id
        self.assertIsNotNone(self.eng.identities.get(
            f"fund:{FUND}:A-USD:USD"))

    # 3 — accumulation vs distribution class
    def test_distribution_policy_distinction(self):
        _ident(self.eng, CLS_A, dist="accumulation")
        _ident(self.eng, "B", dist="distribution")
        a = self.eng.identities.get(IID_A)
        b = self.eng.identities.get(f"fund:{FUND}:B:TWD")
        self.assertEqual(a["distribution_policy"], "accumulation")
        self.assertEqual(b["distribution_policy"], "distribution")

    # 4 — NAV history import (rows)
    def test_nav_import_validation(self):
        rows = [
            {"fund_id": FUND, "share_class_id": CLS_A,
             "nav_date": "2026-09-20", "nav": "25.10", "currency": "TWD"},
            {"fund_id": FUND, "share_class_id": CLS_A,
             "nav_date": "2026-09-21", "nav": "25.40", "currency": "TWD"},
            {"fund_id": FUND, "share_class_id": CLS_A,
             "nav_date": "2026-09-22", "nav": "-1"},   # rejected
        ]
        valid, rejected = import_nav_rows(rows, "tdcc")
        self.assertEqual(len(valid), 2)
        self.assertEqual(len(rejected), 1)

    # 5 — nav_date vs published_at distinct
    def test_nav_date_vs_published(self):
        n = FundNAV(fund_id=FUND, share_class_id=CLS_A,
                    nav_date=date(2026, 9, 22), nav="25.4",
                    currency="TWD", source_id="tdcc",
                    published_at=None)
        self.eng.nav.record(n)
        latest = self.eng.nav.latest_published(FUND, CLS_A)
        self.assertEqual(latest["nav"]["nav_date"], "2026-09-22")
        self.assertIsNone(latest["nav"]["published_at"])
        self.assertIn("非即時", latest["note"])

    # 6 — NAV correction via revision
    def test_nav_revision_correction(self):
        self.eng.nav.record(_nav(FUND, CLS_A, 22, "25.4"))
        res = self.eng.nav.record(_nav(FUND, CLS_A, 22, "25.6", rev=2))
        self.assertTrue(res["corrected"])
        self.assertEqual(
            self.eng.nav.latest(FUND, CLS_A).nav, Decimal("25.6"))
        self.assertEqual(len(self.eng.nav.revisions()), 1)
        # stale revision rejected
        res2 = self.eng.nav.record(_nav(FUND, CLS_A, 22, "25.9", rev=1))
        self.assertTrue(res2["duplicate"])

    # 7 — total return incl. distributions
    def test_total_return_with_distributions(self):
        for d, v in ((10, "100"), (20, "98"), (28, "102")):
            self.eng.nav.record(_nav(FUND, CLS_A, d, v))
        self.eng.distributions.record(FundDistribution(
            fund_id=FUND, share_class_id=CLS_A,
            ex_distribution_date=date(2026, 9, 15),
            amount_per_unit="2", currency="TWD",
            distribution_source="income", source_id="tdcc"))
        series = self.eng.distributions.total_return_navs(
            self.eng.nav.history(FUND, CLS_A), CLS_A)
        self.assertEqual(series[0]["total_return_value"], "100")
        self.assertEqual(series[-1]["total_return_value"], "104")
        # NAV-only return +2%; with-dividend +4%
        perf = self.eng.performance.period_return(
            FUND, CLS_A, "1m", include_distributions=True,
            as_of=date(2026, 9, 28))
        self.assertTrue(perf["ok"])
        self.assertAlmostEqual(float(perf["return"]), 0.04, places=2)

    # 8 — principal distribution identified separately
    def test_principal_distribution_flagged(self):
        self.eng.distributions.record(FundDistribution(
            fund_id=FUND, share_class_id=CLS_B,
            ex_distribution_date=date(2026, 9, 15),
            amount_per_unit="0.5", currency="USD",
            distribution_source=DistributionSource.PRINCIPAL.value,
            source_id="fund-company"))
        t = self.eng.distributions.totals(CLS_B)
        self.assertEqual(t["principal_per_unit"], "0.5")
        self.assertEqual(t["income_per_unit"], "0")

    # 9 — no double-deduction of NAV-embedded fees
    def test_fee_double_count_guard(self):
        self.eng.fees.register(FundFee(
            fund_id=FUND, share_class_id=CLS_A, kind=FeeKind.SUBSCRIPTION.value,
            calc=FeeCalc.PERCENT.value, rate=Decimal("0.03")))
        self.eng.fees.register(FundFee(
            fund_id=FUND, share_class_id=CLS_A, kind=FeeKind.MANAGEMENT.value,
            calc=FeeCalc.PERCENT.value, rate=Decimal("0.015")))
        res = self.eng.fees.transaction_fees(
            FUND, CLS_A, (), "100000")
        self.assertEqual(res["investor_paid_total"], "3000.00")
        # management fee reported as embedded, not charged again
        self.assertEqual(res["nav_embedded"][0]["kind"], "management")
        self.assertNotIn("management",
                         [c["kind"] for c in res["charged"]])

    # 10 — same-basis fund comparison
    def test_fund_comparison_same_basis(self):
        for d in range(1, 25):
            self.eng.nav.record(_nav(FUND, CLS_A, min(d, 28), str(20 + d * 0.1)))
        self.eng.nav.record(_nav("F002", "A", 1, "50"))
        res = self.eng.comparison.compare(
            [(FUND, CLS_A), ("F002", "A")], period="1m")
        self.assertEqual(len(res["results"]), 1)      # F002 lacks data
        self.assertEqual(res["rejected"][0]["error"], "INSUFFICIENT_DATA")
        self.assertEqual(res["parameters"]["period"], "1m")

    # 11 — fund overlap analysis
    def test_fund_overlap(self):
        self.eng.exposure.import_holdings("F1", [
            {"holding_id": "tw:TW_STOCK:2330:TWD", "name": "台積電",
             "weight": "0.10", "sector": "tech"},
            {"holding_id": "us:US_STOCK:AAPL:USD", "name": "Apple",
             "weight": "0.08"}],
            "prospectus", date(2026, 6, 30))
        self.eng.exposure.import_holdings("F2", [
            {"holding_id": "tw:TW_STOCK:2330:TWD", "name": "台積電",
             "weight": "0.12"}],
            "prospectus", date(2026, 6, 30))
        ov = self.eng.exposure.overlap("F1", "F2")
        self.assertEqual(ov["overlap_count"], 1)
        self.assertEqual(ov["shared_holdings"][0]["holding_id"],
                         "tw:TW_STOCK:2330:TWD")

    # 12-13 — direct stock + fund look-through merged, not double counted
    def test_unified_exposure_lookthrough(self):
        self.eng.exposure.import_holdings("TECH-FUND", [
            {"holding_id": "tw:TW_STOCK:2330:TWD", "name": "台積電",
             "weight": "0.20"},
            {"holding_id": "us:US_STOCK:AAPL:USD", "name": "Apple",
             "weight": "0.15"}],
            "prospectus", date(2026, 6, 30))
        res = self.eng.unified.analyze(
            direct_positions=[
                {"instrument_id": "tw:TW_STOCK:2330:TWD",
                 "market_value": "500000", "currency": "TWD",
                 "market": "tw"}],
            fund_positions=[{"fund_id": "TECH-FUND",
                             "market_value": "1000000", "currency": "TWD"}],
            cash=[{"amount": "200000", "currency": "TWD"}],
        )
        total = Decimal(res["total_assets"])
        self.assertEqual(total, Decimal("1700000"))   # fund counted once
        # 台積電 direct 500k + look-through 200k → combined exposure 700k
        self.assertAlmostEqual(
            float(res["concentration"]["tw:TW_STOCK:2330:TWD"]) * 1700000,
            700000, delta=1)
        # AAPL exposure only via fund — visible but not directly sellable
        self.assertIn("us:US_STOCK:AAPL:USD",
                      res["look_through_components"])

    # 14 — partial redemption cost basis
    def test_partial_redemption_cost(self):
        self.eng.nav.record(_nav(FUND, CLS_A, 20, "25"))
        self.eng.nav.record(_nav(FUND, CLS_A, 28, "30"))
        for units, amount in (("100", "2500"),):
            t = FundTransaction(
                account_id=ACCT, fund_id=FUND, share_class_id=CLS_A,
                transaction_type="subscribe", amount=amount, units=units,
                confirmed_nav="25", currency="TWD")
            self.eng.transactions.import_settled(t)
        sell = FundTransaction(
            account_id=ACCT, fund_id=FUND, share_class_id=CLS_A,
            transaction_type="redeem", amount="1500", units="50",
            confirmed_nav="30", currency="TWD")
        self.eng.transactions.import_settled(sell)
        b = self.eng.cost_basis.basis(ACCT, FUND, CLS_A)
        self.assertEqual(Decimal(b["units"]), Decimal("50"))
        self.assertEqual(Decimal(b["avg_cost_per_unit"]), Decimal("25"))
        self.assertEqual(Decimal(b["realized_pnl"]), Decimal("250"))
        self.assertEqual(Decimal(b["unrealized_pnl"]), Decimal("250"))

    # 15 — recurring plan cost tracking
    def test_recurring_cost(self):
        self.eng.nav.record(_nav(FUND, CLS_A, 20, "25"))
        for nav_v, amt in (("20", "3000"), ("25", "3000"), ("30", "3000")):
            units = Decimal(amt) / Decimal(nav_v)
            self.eng.transactions.import_settled(FundTransaction(
                account_id=ACCT, fund_id=FUND, share_class_id=CLS_A,
                transaction_type="recurring", amount=amt,
                units=str(units), confirmed_nav=nav_v, currency="TWD"))
        b = self.eng.cost_basis.basis(ACCT, FUND, CLS_A)
        self.assertEqual(Decimal(b["invested_principal"]), Decimal("9000"))
        # units: 150+120+100=370; avg cost 9000/370 ≈ 24.32
        self.assertEqual(Decimal(b["units"]), Decimal("370"))

    # 16 — fund switch (out-leg redeem + in-leg subscribe)
    def test_fund_switch(self):
        self.eng.nav.record(_nav(FUND, CLS_A, 20, "25"))
        self.eng.nav.record(_nav("F002", "A", 20, "50"))
        self.eng.transactions.import_settled(FundTransaction(
            account_id=ACCT, fund_id=FUND, share_class_id=CLS_A,
            transaction_type="subscribe", amount="2500", units="100",
            confirmed_nav="25", currency="TWD"))
        sw = FundTransaction(
            account_id=ACCT, fund_id=FUND, share_class_id=CLS_A,
            transaction_type="switch", amount="2500", units="100",
            confirmed_nav="25", currency="TWD",
            note="switch→F002")
        res = self.eng.transactions.import_settled(sw)
        self.assertTrue(res["ok"])
        # treated as buy-leg on record; F002 leg recorded separately
        b = self.eng.cost_basis.basis(ACCT, FUND, CLS_A)
        self.assertGreaterEqual(Decimal(b["units"]), Decimal("0"))

    # 17 — redemption proceeds not credited before settlement
    def test_redemption_no_early_cash(self):
        t = FundTransaction(
            account_id=ACCT, fund_id=FUND, share_class_id=CLS_A,
            transaction_type="redeem", amount="5000", currency="TWD",
            status="DRAFT")
        res = self.eng.transactions.create(t)
        tid = res["transaction"]["transaction_id"]
        for target in ("SUBMITTED", "ACCEPTED", "PRICING_PENDING"):
            res = self.eng.transactions.transition(tid, target)
            self.assertTrue(res["ok"])
        pending = self.eng.transactions.pending_settlement(ACCT)
        # priced but not settled — shows in pending, not credited
        res = self.eng.transactions.transition(
            tid, "PRICED", confirmed_nav="26")
        self.assertTrue(res["ok"])
        self.assertEqual(len(self.eng.transactions.pending_settlement()), 1)
        res = self.eng.transactions.transition(tid, "SETTLED")
        self.assertFalse(res["ok"])   # must go through SETTLEMENT_PENDING
        self.eng.transactions.transition(tid, "SETTLEMENT_PENDING")
        res = self.eng.transactions.transition(tid, "SETTLED")
        self.assertTrue(res["ok"])
        self.assertEqual(self.eng.transactions.pending_settlement(), [])

    # 18 — stale NAV flagged
    def test_stale_nav_warning(self):
        old = date.today() - timedelta(days=30)
        self.eng.nav.record(FundNAV(
            fund_id=FUND, share_class_id=CLS_A, nav_date=old,
            nav="20", currency="TWD", source_id="tdcc"))
        latest = self.eng.nav.latest_published(FUND, CLS_A)
        self.assertTrue(latest["stale"])
        self.assertGreater(latest["age_days"], 5)

    # 19 — 星澄 analysis surface via query + recommendation
    def test_xingcheng_fund_analysis(self):
        _ident(self.eng, CLS_A)
        for d in range(1, 25):
            self.eng.nav.record(_nav(FUND, CLS_A, min(d, 28), str(20 + d)))
        perf = self.eng.performance.all_periods(FUND, CLS_A)
        self.assertTrue(perf["ok"])
        self.assertTrue(perf["periods"]["1m"]["ok"])

    # 20 — recommendation traceability
    def test_recommendation_traceable(self):
        self.eng.nav.record(_nav(FUND, CLS_A, 22, "25"))
        rec = FundRecommendation(
            fund_id=FUND, share_class_id=CLS_A, account_id=ACCT,
            recommendation_type="HOLD",
            analysis_date=date(2026, 9, 23), nav_date=date(2026, 9, 22),
            reasoning="波動在正常範圍", data_sources=["tdcc", "prospectus"],
            model_id="star-native", model_version="v3",
            strategy_version="alloc-v1",
            risk_factors=["單一產業集中"])
        res = self.eng.recommendations.record(rec)
        self.assertTrue(res["ok"])
        self.assertEqual(res["nav_basis"]["nav"], Decimal("25"))
        # recommendation without NAV basis is rejected
        bad = FundRecommendation(
            fund_id=FUND, share_class_id=CLS_A, account_id=ACCT,
            recommendation_type="SUBSCRIBE",
            analysis_date=date(2026, 9, 23), nav_date=None,
            reasoning="", data_sources=[], model_id="star-native")
        res2 = self.eng.recommendations.record(bad)
        self.assertFalse(res2["ok"])

    # 21 — platform/provider isolation (per-source data, manual only)
    def test_provider_isolation(self):
        caps = {c.provider_id: c for c in PROVIDER_CAPABILITIES.values()}
        self.assertFalse(caps["tdcc"].api)
        self.assertTrue(caps["manual-import"].verified)
        self.assertFalse(caps["tdcc"].verified)

    # 22 — PG schema integrity
    def test_pg_schema_integrity(self):
        sql = (TOOL_ROOT.parents[1] / "shared-layer" / "migrations"
               / "136_fund_data.sql").read_text("utf-8")
        for table in ("fund", "fund_share_class", "fund_nav",
                      "fund_distribution", "fund_fee", "fund_holding",
                      "fund_exposure", "fund_transaction",
                      "fund_cost_basis", "fund_performance",
                      "fund_recommendation", "fund_strategy",
                      "fund_sync_state"):
            self.assertIn(table, sql, table)
        self.assertIn("ROW LEVEL SECURITY", sql)

    # 23 — no legacy portfolio-manager residue
    def test_no_legacy_residue(self):
        trading = (TOOL_ROOT / "src/backend/services/investment_mobile"
                   / "trading")
        names = [p.name for p in trading.iterdir() if p.is_file()]
        self.assertNotIn("fund.py", names)   # old ledger retired
        self.assertTrue((trading / "fund").is_dir())

    # 24 — live fund trading disabled
    def test_fund_live_disabled(self):
        self.assertEqual(self.eng.status()["live_trading"], "disabled")
        # fund transactions are records only — no OMS path exists
        self.assertFalse(hasattr(self.eng, "oms"))

    # bonus — status machine rejects illegal jump
    def test_illegal_transition(self):
        t = FundTransaction(
            account_id=ACCT, fund_id=FUND, share_class_id=CLS_A,
            transaction_type="subscribe", amount="3000", currency="TWD")
        res = self.eng.transactions.create(t)
        tid = res["transaction"]["transaction_id"]
        bad = self.eng.transactions.transition(tid, "SETTLED")
        self.assertFalse(bad["ok"])
        self.assertEqual(bad["error_code"], "ILLEGAL_TRANSITION")


if __name__ == "__main__":
    unittest.main(verbosity=2)
