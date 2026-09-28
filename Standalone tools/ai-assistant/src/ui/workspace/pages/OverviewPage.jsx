import React, { useMemo } from "react";
import { useCommandQuery } from "../hooks.js";
import { DataFreshnessIndicator } from "../freshness.jsx";
import { InvestmentChart } from "../chart.jsx";
import { EmptyState, Metric, Money, Pnl, Section } from "../common.jsx";
const arr = (v) => Array.isArray(v) ? v : [];
const num = (v) => {
	const n = Number(v);
	return Number.isFinite(n) ? n : 0;
};
export default function OverviewPage({ sendCommand }) {
	const summary = useCommandQuery(sendCommand, "investment_assets_summary");
	const valuation = useCommandQuery(sendCommand, "investment_assets_valuation");
	const allocation = useCommandQuery(sendCommand, "investment_assets_allocation");
	const monitor = useCommandQuery(sendCommand, "investment_monitor_overview");
	const v = valuation.data?.valuation ?? {};
	const markets = summary.data?.markets ?? {};
	const alloc = allocation.data?.allocation ?? {};
	const allocSlices = useMemo(() => Object.entries(markets).map(([k, val]) => ({
		label: k.toUpperCase(),
		value: num(val)
	})), [markets]);
	const equityCurve = useMemo(() => arr(v.history || v.equity_curve).map((p, i) => ({
		x: p.date || i,
		y: num(p.value ?? p.equity)
	})), [v]);
	const cashflow = useMemo(() => arr(v.cashflow || v.cash_flow).map((p) => ({
		label: String(p.month || p.date || ""),
		value: num(p.amount ?? p.net)
	})), [v]);
	const anyData = summary.data || valuation.data || allocation.data;
	if (!anyData && !summary.loading && !valuation.loading) return <EmptyState kind="not_integrated" detail="資產鏡像尚未由引擎送達。" />;
	return <div className="inv-page">
      <header className="inv-page-head">
        <h2>全資產首頁</h2>
        <DataFreshnessIndicator record={v} source="FILE_IMPORT" />
      </header>

      <div className="inv-metric-grid">
        <Metric label="總資產" value={<Money value={v.total} currency="TWD" />} sub={`估值日期 ${v.valuation_date || "—"}`} />
        <Metric label="台股資產" value={<Money value={v.tw ?? markets["tw"]} currency="TWD" />} />
        <Metric label="美股資產" value={<Money value={v.us ?? markets["us"]} currency="USD" />} />
        <Metric label="共同基金" value={<Money value={v.fund ?? markets["fund"]} currency="TWD" />} />
        <Metric label="台幣現金" value={<Money value={v.cash_twd} currency="TWD" />} />
        <Metric label="美元現金" value={<Money value={v.cash_usd} currency="USD" />} />
      </div>

      <div className="inv-metric-grid">
        <Metric label="累計損益" value={<Pnl value={v.total_pnl} />} tone={num(v.total_pnl) > 0 ? "up" : num(v.total_pnl) < 0 ? "down" : "flat"} />
        <Metric label="已實現損益" value={<Pnl value={v.realized_pnl} />} />
        <Metric label="未實現損益" value={<Pnl value={v.unrealized_pnl} />} />
        <Metric label="累計股息" value={<Money value={v.dividends} currency="TWD" />} />
        <Metric label="累計配息" value={<Money value={v.distributions} currency="TWD" />} />
      </div>

      <div className="inv-chart-grid">
        <InvestmentChart kind="area" title="總資產變化" points={equityCurve} empty="歷史估值不足——資料不足" />
        <InvestmentChart kind="donut" title="資產配置" slices={allocSlices} empty="尚無配置資料" />
        <InvestmentChart kind="donut" title="產業配置" slices={arr(alloc.by_sector).map((s) => ({
		label: String(s.sector || s.label),
		value: num(s.value ?? s.weight)
	}))} empty="尚無產業資料" />
        <InvestmentChart kind="donut" title="幣別配置" slices={arr(alloc.by_currency).map((s) => ({
		label: String(s.currency || s.label),
		value: num(s.value ?? s.weight)
	}))} empty="尚無幣別資料" />
        <InvestmentChart kind="bars" title="投資現金流" bars={cashflow} empty="尚無現金流資料" />
      </div>

      <Section title="資料狀態" aside={<DataFreshnessIndicator record={monitor.data} />}>
        {monitor.data ? <pre className="inv-json">{JSON.stringify(monitor.data, null, 2)}</pre> : <EmptyState kind="insufficient" detail="監測狀態尚未送達。" />}
      </Section>
      <p className="inv-note">
        不同來源的估值日期可能不同——各筆資料時間以標記為準，非同步即時資產。
      </p>
    </div>;
}
