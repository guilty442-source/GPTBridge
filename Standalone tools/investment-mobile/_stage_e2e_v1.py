# -*- coding: utf-8 -*-
"""Phase-14 V1.0 formal end-to-end acceptance.

One reproducible pipeline on local synthetic data — no real broker
transport, no LIVE path, no real funds:

  start -> offline accounts -> import holdings -> market data
  -> total assets -> Xingcheng analysis -> recommendations
  -> strategy -> backtest -> SHADOW -> PAPER -> sim fills
  -> performance -> report -> stop -> restart -> consistency
"""
import asyncio
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "backend" / "services"))

from investment_mobile.trading.engine_service import TradingEngineService
from investment_mobile.trading.market.contracts import MarketCandle

ALL_SESSIONS = {"sessions": ["regular", "pre", "post", "closed"]}


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class E2EV1(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._close)
        self.svc = TradingEngineService(Path(self._tmp.name))
        self.svc.candle_store.open()

    def _close(self):
        try:
            self.svc.close()
        except Exception:
            pass
        self._tmp.cleanup()

    def call(self, cmd, payload=None):
        return _run(self.svc.handle(cmd, payload or {}))[1]

    # ---------------- helpers ----------------
    def _accounts(self):
        for kind, aid in (("cathay-tw", "CATHAY_TW"),
                          ("fubon-us", "FUBON_US"),
                          ("fund", "MUTUAL_FUND")):
            r = self.call("investment-mobile-offline-account-create",
                          {"kind": kind, "label": aid,
                           "account_id": aid})
            self.assertTrue(r["ok"], r)

    def _seed_candles(self, iid="2330", market="tw", days=60,
                      base=500.0, step=2.0):
        end = datetime.now(timezone.utc)
        rows = []
        for i in range(days):
            d = end - timedelta(days=days - 1 - i)
            c = base + step * i
            rows.append(MarketCandle(
                instrument_id=iid, market=market, timeframe="1d",
                open=c - 1, high=c + 2, low=c - 3, close=c,
                volume=100000, candle_start=d,
                candle_end=d + timedelta(hours=12),
                source_id="test"))
        self.svc.candle_store.upsert_candles(rows)

    def _auto_register(self, sid, mode, account=""):
        r = self.call(
            "investment-mobile-autotrade-strategy-register",
            {"strategy_id": sid, "strategy_version": 1,
             "market": "tw", "instrument_scope": ["2330"],
             "strategy_type": "MOMENTUM", "execution_mode": mode,
             "account_id": account, "ai_policy": "DETERMINISTIC",
             "session_policy": ALL_SESSIONS, "parameters": {}})
        self.assertTrue(r["ok"], r)
        return r["run"]["run_id"]

    def _auto_start(self, run_id):
        for t in ("READY", "RUNNING"):
            r = self.call(
                "investment-mobile-autotrade-strategy-transition",
                {"run_id": run_id, "target": t, "actor": "user"})
            self.assertTrue(r["ok"], r)

    def _auto_event(self, rev="r1"):
        return self.call("investment-mobile-autotrade-event",
                         {"event_type": "CANDLE_CLOSED",
                          "market": "tw", "instrument_id": "2330",
                          "source_id": "test", "data_revision": rev})

    # ============ full pipeline ============
    def test_01_full_pipeline(self):
        self._accounts()
        for itype, mkt, sym, ccy in (
                ("TW_STOCK", "tw", "2330", "TWD"),
                ("US_STOCK", "us", "AAPL", "USD")):
            r = self.call("investment-mobile-instrument-register",
                          {"instrument_type": itype, "market": mkt,
                           "symbol": sym, "currency": ccy})
            self.assertTrue(r["ok"], r)
        self.call("investment-mobile-offline-holding-set",
                  {"account_id": "CATHAY_TW", "instrument_id": "2330",
                   "quantity": "1000", "avg_cost": "500"})
        self.call("investment-mobile-offline-cash-set",
                  {"account_id": "CATHAY_TW", "currency": "TWD",
                   "amount": "50000"})
        self.call("investment-mobile-offline-holding-set",
                  {"account_id": "FUBON_US", "instrument_id": "AAPL",
                   "quantity": "10", "avg_cost": "180"})
        self.call("investment-mobile-fx-rate-record",
                  {"base": "USD", "quote": "TWD", "rate": "32.0",
                   "source_id": "manual"})
        self._seed_candles()
        val = self.call("investment-mobile-asset-value",
                        {"prices": {"2330": "600", "AAPL": "200"}})
        self.assertTrue(val["ok"], val)
        self.assertTrue(val["total"] > 0)
        uni = self.call("investment-mobile-portfolio-unified",
                        {"display_currency": "TWD"})
        self.assertTrue(uni["ok"], uni)
        ai = self.call("investment-mobile-ai-analyze-tw",
                       {"instrument_id": "2330"})
        self.assertTrue(ai["ok"], ai)
        s = self.call("investment-mobile-strategy-register", {
            "strategy_id": "e2e-mom", "strategy_name": "e2e-mom",
            "strategy_type": "MOMENTUM", "market": "TAIWAN_EQUITY",
            "instrument_scope": ["2330"],
            "parameters": {"fast": 3, "slow": 8, "quantity": 10}})
        self.assertTrue(s["ok"], s)
        bt = self.call("investment-mobile-backtest-submit", {
            "job_id": "e2e-bt", "strategy_id": "e2e-mom",
            "start_date": "2025-01-01", "end_date": "2025-02-01",
            "initial_capital": "100000"})
        self.assertTrue(bt["ok"], bt)
        # SHADOW — signal only, never a fill
        sh = self._auto_register("e2e-shadow", "SHADOW")
        self._auto_start(sh)
        self._auto_event("sh-1")
        runs = self.call("investment-mobile-autotrade-strategy-list",
                         {})["strategies"]
        sh_run = [r for r in runs if r["run_id"] == sh][0]
        self.assertEqual(sh_run["state"], "RUNNING")
        self.assertEqual(self.svc.sim.orders.list(), [])
        # PAPER — simulated fill
        self.call("investment-mobile-paper-account-create",
                  {"account_id": "paper-tw", "market": "tw",
                   "base_currency": "TWD",
                   "initial_capital": "1000000"})
        self.call("investment-mobile-autotrade-capital-set",
                  {"plan": {"allocations": {"e2e-paper": "0.4"},
                            "reserve_cash": "0.3"}})
        pp = self._auto_register("e2e-paper", "PAPER",
                                 account="paper-tw")
        self._auto_start(pp)
        self._auto_event("pp-1")
        perf = self.call("investment-mobile-autotrade-performance",
                         {"run_id": pp})
        self.assertTrue(perf["ok"], perf)
        self.assertTrue(perf["snapshot"]["simulated"])
        self.assertEqual(perf["snapshot"]["filled"], 1)
        rep = self.call("investment-mobile-autotrade-report",
                        {"run_id": pp})
        self.assertTrue(rep["ok"], rep)
        # PAPER must not leak into the manual (real) position book
        pos = self.call("investment-mobile-asset-position-list",
                        {"account_id": "CATHAY_TW"})
        qtys = {p["instrument_id"]: str(p["quantity"])
                for p in pos["positions"]}
        self.assertEqual(qtys.get("2330"), "1000")

    # ============ restart consistency ============
    def test_02_restart_no_duplicate_fills(self):
        self.call("investment-mobile-trading-mode-set",
                  {"mode": "PAPER"})
        self.call("investment-mobile-paper-account-create",
                  {"account_id": "paper-tw", "market": "tw",
                   "base_currency": "TWD",
                   "initial_capital": "1000000"})
        self._seed_candles()
        self.call("investment-mobile-autotrade-capital-set",
                  {"plan": {"allocations": {"s1": "0.4"},
                            "reserve_cash": "0.3"}})
        r1 = self._auto_register("s1", "PAPER", account="paper-tw")
        self._auto_start(r1)
        self._auto_event("r1")
        before = self.call("investment-mobile-autotrade-performance",
                           {"run_id": r1})["snapshot"]["filled"]
        # crash -> restart on the same state dir
        self.svc.close()
        svc2 = TradingEngineService(Path(self._tmp.name))
        try:
            svc2.candle_store.open()
            svc2.autotrade.maintenance.recover()
            runs = svc2.autotrade.strategies.list()
            self.assertEqual(len(runs), 1)
            # recovery must not blindly resume RUNNING / replay signals
            self.assertEqual(runs[0]["state"], "PAUSED")
            after = svc2.autotrade.performance.snapshot(r1)["filled"]
            self.assertEqual(after, before)
        finally:
            svc2.close()
        self.svc = TradingEngineService(Path(self._tmp.name))
        self.svc.candle_store.open()

    # ============ acceptance matrix ============
    def test_03_acceptance_matrix(self):
        r = self.call("investment-mobile-perf-acceptance", {})
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["counts"]["FAIL"], 0,
                         [f for f in r["features"]
                          if f["status"] == "FAIL"])
        self.assertGreaterEqual(r["counts"]["PASS"], 25)
        blocked = [f["feature_id"] for f in r["features"]
                   if f["status"] == "BLOCKED"]
        self.assertIn("pg.persistence", blocked)
        self.assertIn("ui.control_route", blocked)

    # ============ offline safety ============
    def test_04_offline_safety(self):
        g = self.call("investment-mobile-offline-gate-status", {})
        self.assertFalse(g["broker_network_enabled"])
        live = self.call("investment-mobile-live-status", {})
        self.assertTrue(live["phase_locked"])
        self.assertFalse(live["dispatch_enabled"])
        self.call("investment-mobile-trading-mode-set",
                  {"mode": "ANALYSIS"})
        r = self.call("investment-mobile-paper-order-submit",
                      {"account_id": "paper-tw",
                       "instrument_id": "2330", "side": "buy",
                       "quantity": "10"})
        self.assertEqual(r["error_code"], "MODE_BLOCKED")

    # ============ fund stays off the equity fill path ============
    def test_05_fund_no_equity_fills(self):
        self.call("investment-mobile-trading-mode-set",
                  {"mode": "PAPER"})
        from investment_mobile.trading.fund.contracts import FundNAV
        from datetime import date
        self.svc.fund_engine.nav.record(FundNAV(
            fund_id="F1", share_class_id="A",
            nav_date=date.today(), nav="10.5",
            currency="TWD", source_id="test"))
        r = self.call("investment-mobile-autotrade-fund-analyze",
                      {"fund_id": "F1", "share_class_id": "A"})
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["pricing_model"],
                         "published-nav (基金專屬計價)")
        self.assertEqual(self.svc.sim.orders.list(), [])

    # ============ risk evidence gate ============
    def test_06_risk_incomplete_evidence(self):
        self.call("investment-mobile-trading-mode-set",
                  {"mode": "PAPER"})
        self.call("investment-mobile-paper-account-create",
                  {"account_id": "paper-tw", "market": "tw",
                   "base_currency": "TWD",
                   "initial_capital": "1000"})
        # cash far below the order value -> must be blocked, never ALLOW
        r = self.call("investment-mobile-paper-order-submit",
                      {"account_id": "paper-tw",
                       "instrument_id": "2330", "side": "buy",
                       "quantity": "100", "price": "600"})
        self.assertFalse(r["ok"])
        self.assertNotEqual(r.get("error_code"), "ALLOW")


if __name__ == "__main__":
    unittest.main()
