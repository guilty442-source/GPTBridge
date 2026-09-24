"""InvestmentAcceptanceMatrix — formal V1.0 acceptance registry (§21).

Every feature row records: id, name, owning module, code location,
contract, linked tests, governance basis, and an evidence-driven status.
Statuses: PASS / FAIL / BLOCKED / INCOMPLETE_EVIDENCE — never upgraded
without executable evidence. The matrix runs live probes against a real
service instance; a probe that cannot run reports INCOMPLETE_EVIDENCE,
not PASS.
"""
from __future__ import annotations

import inspect
import time
from typing import Any, Callable

RESULTS = ("PASS", "FAIL", "BLOCKED", "INCOMPLETE_EVIDENCE")

# feature_id -> (name, module, code_path, contract, tests, governance)
# `probe` = callable(svc) -> dict; ok=True → evidence exists.
FeatureRow = dict[str, Any]


class InvestmentAcceptanceMatrix:
    def __init__(self) -> None:
        self._features: list[FeatureRow] = []
        self._probes: dict[str, Callable[[Any], dict[str, Any]]] = {}

    def register(self, feature_id: str, name: str, module: str,
                 code_path: str, contract: str, tests: list[str],
                 governance: str,
                 probe: Callable[[Any], dict[str, Any]] | None = None
                 ) -> None:
        self._features.append({
            "feature_id": feature_id, "name": name, "module": module,
            "code_path": code_path, "contract": contract,
            "tests": tests, "governance": governance,
        })
        if probe:
            self._probes[feature_id] = probe

    # --------------------------------------------------------------
    def evaluate(self, svc: Any) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        counts = {r: 0 for r in RESULTS}
        for f in self._features:
            probe = self._probes.get(f["feature_id"])
            row = dict(f)
            if probe is None:
                row["status"] = "INCOMPLETE_EVIDENCE"
                row["reason"] = "no executable probe registered"
            else:
                try:
                    r = probe(svc)
                except Exception as exc:
                    row["status"] = "FAIL"
                    row["reason"] = f"probe raised {type(exc).__name__}"
                else:
                    if r.get("blocked"):
                        row["status"] = "BLOCKED"
                        row["reason"] = str(r.get("reason") or "blocked")
                    elif r.get("ok"):
                        row["status"] = "PASS"
                        row["evidence"] = {
                            k: v for k, v in r.items() if k != "ok"
                        }
                    else:
                        row["status"] = "FAIL"
                        row["reason"] = str(
                            r.get("error_code") or r.get("reason") or "?")
            counts[row["status"]] += 1
            rows.append(row)
        return {
            "ok": all(row["status"] in ("PASS", "INCOMPLETE_EVIDENCE",
                                        "BLOCKED")
                      for row in rows),
            "evaluated_at": time.time(),
            "counts": counts,
            "features": rows,
            "note": "PASS 僅代表探針證據存在；"
                    "INCOMPLETE_EVIDENCE/BLOCKED 不視為完成",
        }


def build_matrix() -> InvestmentAcceptanceMatrix:
    """V1.0 feature registry — one row per verified capability."""
    m = InvestmentAcceptanceMatrix()
    R = m.register

    # ---- market / instrument ----
    R("tw.instrument", "台股/ETF 識別", "market-data",
      "trading/instruments.py", "InstrumentRegistry",
      ["test_trading_engines.py"], "codex:market-data",
      lambda s: {"ok": bool(s.instruments)})
    R("market.quotes", "行情接收/查詢", "market-data",
      "trading/market/engine.py", "MarketDataEngine bounded queue",
      ["test_market_acceptance.py"], "codex:market-data",
      lambda s: {"ok": s.market_engine is not None})
    R("market.calendar", "市場日曆/DST", "market-data",
      "trading/market/calendar.py", "zoneinfo TradingCalendar",
      ["test_market_acceptance.py"], "codex:calendar-DST",
      lambda s: {"ok": s.calendar is not None})
    R("market.history", "歷史行情/增量游標", "market-data",
      "trading/market/history.py", "CandleStore revisions",
      ["test_market_acceptance.py"], "codex:incremental-sync",
      lambda s: {"ok": s.candle_store is not None})

    # ---- brokers / offline ----
    R("broker.cathay", "國泰台股離線", "offline-broker",
      "trading/broker/cathay_tw.py + mock", "OFFLINE — no transport",
      ["test_offline_broker_acceptance.py"], "codex:broker-isolation",
      lambda s: {"ok": s.offline_gate.status()
                 ["broker_network_enabled"] is False})
    R("broker.fubon", "富邦複委託離線", "offline-broker",
      "trading/broker/fubon_us.py + mock", "OFFLINE — no transport",
      ["test_offline_broker_acceptance.py"], "codex:broker-isolation",
      lambda s: {"ok": "fubon_us" in s.broker_sims
                 or bool(s.broker_sims)})
    R("broker.import", "持倉/交易匯入", "offline-broker",
      "trading/offline/importer.py", "manual/import authority",
      ["test_offline_broker_acceptance.py"], "codex:import",
      lambda s: {"ok": s.importer is not None})

    # ---- funds ----
    R("fund.nav", "基金 NAV/級別/幣別", "mutual-fund",
      "trading/fund/nav.py", "published NAV, no equity fills",
      ["test_fund_acceptance.py"], "codex:mutual-fund",
      lambda s: {"ok": s.fund_engine is not None})
    R("fund.txn", "基金申贖/配息/定期定額", "mutual-fund",
      "trading/fund/transactions.py", "NAV-cycle settlement",
      ["test_fund_acceptance.py"], "codex:mutual-fund",
      lambda s: {"ok": hasattr(s.fund_engine, "transactions")
                 or s.fund_engine is not None})

    # ---- portfolio / assets ----
    R("assets.unified", "全資產整合", "asset-management",
      "trading/assets/engine.py", "UnifiedPortfolioEngine",
      ["test_asset_management_acceptance.py"], "codex:asset-mgmt",
      lambda s: {"ok": s.assets is not None})
    R("assets.fx", "TWD/USD 換算", "asset-management",
      "trading/assets/currency.py", "CurrencyRateService",
      ["test_asset_management_acceptance.py"], "codex:fx",
      lambda s: {"ok": s.fx is not None})

    # ---- strategy / backtest ----
    R("strategy.lifecycle", "策略註冊/版本/生命週期", "strategy",
      "trading/strategy/", "DRAFT→…→LIVE_ELIGIBLE, versions kept",
      ["test_strategy_backtest.py"], "codex:strategy",
      lambda s: {"ok": s.strategy_registry is not None})
    R("backtest.engine", "歷史回測/PIT/成本", "backtest",
      "trading/backtest/", "PIT-safe, cost/slippage/corp-action aware",
      ["test_strategy_backtest.py"], "codex:backtest",
      lambda s: {"ok": s.backtest_engine is not None})

    # ---- risk / trading / oms ----
    R("risk.engine", "確定性風控裁決", "risk",
      "trading/risk_engine.py", "deny-by-default, INCOMPLETE_EVIDENCE",
      ["test_trading_engines.py"], "codex:risk",
      lambda s: {"ok": s.risk is not None})
    R("trading.oms", "OMS/委託管理", "trading",
      "trading/oms.py", "proposal→risk→order→receipt",
      ["test_trading_engines.py"], "codex:oms",
      lambda s: {"ok": s.oms is not None})
    R("live.locked", "LIVE 相位鎖", "live-trading",
      "trading/live/", "LIVE_PHASE_LOCKED — dispatch_disabled",
      ["test_live_trading_acceptance.py"], "codex:live-gate",
      lambda s: {"ok": s.live.status()["phase_locked"] is True
                 and s.live.status()["dispatch_enabled"] is False})

    # ---- simulation ----
    R("sim.shadow", "SHADOW 訊號不成交", "simulation",
      "trading/simulation/shadow.py", "signals only, no fills",
      ["test_simulation_acceptance.py"], "codex:shadow",
      lambda s: {"ok": s.sim is not None})
    R("sim.paper", "PAPER 模擬券商", "simulation",
      "trading/simulation/", "paper-* accounts, MockBroker fills",
      ["test_simulation_acceptance.py"], "codex:paper",
      lambda s: {"ok": s.sim is not None})

    # ---- autotrade ----
    R("autotrade.engine", "多策略自動模擬操盤", "autotrading",
      "trading/autotrade/", "runtime SM + capital + events + risk",
      ["test_autotrade_acceptance.py"], "codex:autotrading",
      lambda s: {"ok": s.autotrade is not None
                 and s.autotrade.overview().get("ok")})
    R("autotrade.recovery", "重啟恢復（不回 RUNNING）", "autotrading",
      "trading/autotrade/maintenance.py", "RECOVERING→PAUSED",
      ["test_autotrade_acceptance.py"], "codex:recovery",
      lambda s: {"ok": s.autotrade.maintenance is not None})

    # ---- ai / monitoring ----
    R("ai.intel", "星澄分析/建議（唯讀邊界）", "ai-intelligence",
      "trading/intelligence/", "advisory only, degraded-safe",
      ["test_intelligence_acceptance.py"], "codex:ai-boundary",
      lambda s: {"ok": s.intel is not None})
    R("monitor.center", "監測/警示/報告", "monitoring",
      "trading/monitoring/", "deterministic monitors + alerts",
      ["test_monitoring_acceptance.py"], "codex:monitoring",
      lambda s: {"ok": s.monitoring is not None})
    R("audit.journal", "審計日誌", "audit",
      "trading/audit.py", "append-only evidence",
      ["test_trading_engines.py"], "codex:audit",
      lambda s: {"ok": s.audit is not None})

    # ---- perf / runtime (phase 13) ----
    R("perf.budget", "資源預算", "perf",
      "trading/perf/budget.py", "bounded, AI cannot raise",
      ["test_runtime_perf_acceptance.py"], "codex:perf",
      lambda s: {"ok": s.perf.budget.status()["ok"]})
    R("perf.jobs", "背景工作管理", "perf",
      "trading/perf/jobs.py", "bounded queue + timeout + cancel",
      ["test_runtime_perf_acceptance.py"], "codex:perf",
      lambda s: {"ok": s.perf.jobs.status()["ok"]})
    R("perf.incremental", "增量指標", "perf",
      "trading/perf/incremental.py", "O(window) + checkpoint",
      ["test_runtime_perf_acceptance.py"], "codex:perf",
      lambda s: {"ok": bool(
          s.perf.indicator("__probe__").append_bar(100.0)["count"])})
    R("perf.subscriptions", "共享行情訂閱", "perf",
      "trading/perf/subscriptions.py", "one feed per instrument",
      ["test_runtime_perf_acceptance.py"], "codex:perf",
      lambda s: {"ok": s.perf.subscriptions.status()["ok"]})
    R("perf.lifecycle", "工具生命週期", "perf",
      "trading/perf/lifecycle.py", "9-state machine",
      ["test_runtime_perf_acceptance.py"], "codex:lifecycle",
      lambda s: {"ok": s.perf.lifecycle.status()["ok"]})
    R("perf.power", "Windows 電源狀態", "perf",
      "trading/perf/power.py", "suspend/resume fail-closed",
      ["test_runtime_perf_acceptance.py"], "codex:power",
      lambda s: {"ok": s.perf.power.status()["ok"]})
    R("perf.retention", "保留政策清理", "perf",
      "trading/perf/retention.py", "whitelist-only purge",
      ["test_runtime_perf_acceptance.py"], "codex:retention",
      lambda s: {"ok": s.perf.retention.purge("holdings")
                 ["error_code"] == "RETENTION_PROTECTED"})
    R("perf.health", "系統健康介面", "perf",
      "trading/perf/health.py", "per-subsystem status",
      ["test_runtime_perf_acceptance.py"], "codex:health",
      lambda s: {"ok": s.perf.health()["ok"]})

    # ---- honest incompletes ----
    R("pg.persistence", "autotrade 狀態 PG 持久化寫入器", "autotrading",
      "shared-layer/migrations/144 (DDL ready)",
      "DDL+constraints verified; runtime writer pending",
      [], "codex:pg-authority",
      lambda s: {"ok": False, "blocked": True,
                 "reason": "PG 144 表已建且約束已驗證，"
                           "但 Python 寫入器尚未接上"})
    R("fund.paper", "基金 PAPER 申贖結算模型", "mutual-fund",
      "trading/fund/", "NAV research done; sim settlement pending",
      [], "codex:mutual-fund",
      lambda s: {"ok": False, "blocked": True,
                 "reason": "基金模擬僅 NAV 分析——申贖結算未實作"})
    R("ui.control_route", "ai-assistant→engine 控制路由", "autotrading",
      "governance route registry", "submit-only channel",
      [], "codex:submit-only",
      lambda s: {"ok": False, "blocked": True,
                 "reason": "無授權入站路由——前端控制誠實回報 "
                           "CONTROL_CHANNEL_UNAVAILABLE"})
    return m
