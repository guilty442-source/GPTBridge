//! OverviewPage.js — 全資產首頁 (React-free, E180/C116).
//! Same bounded queries + honest empty states as the retired JSX page.
import { h, rerender } from "../../../../../../shared-layer/src/ui/toolWindow/dom.js";
import { dataFreshnessIndicator } from "../freshness.js";
import { investmentChart } from "../chart.js";
import { emptyState, metric, money, pnl, section } from "../common.js";

const arr = (v) => Array.isArray(v) ? v : [];
const num = (v) => {
	const n = Number(v);
	return Number.isFinite(n) ? n : 0;
};

export function mount(ctx) {
	const summary = ctx.query("investment_assets_summary");
	const valuation = ctx.query("investment_assets_valuation");
	const allocation = ctx.query("investment_assets_allocation");
	const monitor = ctx.query("investment_monitor_overview");
	const queries = [summary, valuation, allocation, monitor];
	const el = h("div");
	const render = () => rerender(el, () => {
		const v = valuation.get().data?.valuation ?? {};
		const markets = summary.get().data?.markets ?? {};
		const alloc = allocation.get().data?.allocation ?? {};
		const allocSlices = Object.entries(markets).map(([k, val]) => ({
			label: k.toUpperCase(),
			value: num(val)
		}));
		const equityCurve = arr(v.history || v.equity_curve).map((p, i) => ({
			x: p.date || i,
			y: num(p.value ?? p.equity)
		}));
		const cashflow = arr(v.cashflow || v.cash_flow).map((p) => ({
			label: String(p.month || p.date || ""),
			value: num(p.amount ?? p.net)
		}));
		const anyData = summary.get().data || valuation.get().data || allocation.get().data;
		if (!anyData && !summary.get().loading && !valuation.get().loading) {
			return emptyState({ kind: "not_integrated", detail: "資產鏡像尚未由引擎送達。" });
		}
		return h("div", { className: "inv-page" },
			h("header", { className: "inv-page-head" },
				h("h2", null, "全資產首頁"),
				dataFreshnessIndicator({ record: v, source: "FILE_IMPORT" })),
			h("div", { className: "inv-metric-grid" },
				metric({ label: "總資產", value: money({ value: v.total, currency: "TWD" }), sub: `估值日期 ${v.valuation_date || "—"}` }),
				metric({ label: "台股資產", value: money({ value: v.tw ?? markets["tw"], currency: "TWD" }) }),
				metric({ label: "美股資產", value: money({ value: v.us ?? markets["us"], currency: "USD" }) }),
				metric({ label: "共同基金", value: money({ value: v.fund ?? markets["fund"], currency: "TWD" }) }),
				metric({ label: "台幣現金", value: money({ value: v.cash_twd, currency: "TWD" }) }),
				metric({ label: "美元現金", value: money({ value: v.cash_usd, currency: "USD" }) })),
			h("div", { className: "inv-metric-grid" },
				metric({ label: "累計損益", value: pnl({ value: v.total_pnl }), tone: num(v.total_pnl) > 0 ? "up" : num(v.total_pnl) < 0 ? "down" : "flat" }),
				metric({ label: "已實現損益", value: pnl({ value: v.realized_pnl }) }),
				metric({ label: "未實現損益", value: pnl({ value: v.unrealized_pnl }) }),
				metric({ label: "累計股息", value: money({ value: v.dividends, currency: "TWD" }) }),
				metric({ label: "累計配息", value: money({ value: v.distributions, currency: "TWD" }) })),
			h("div", { className: "inv-chart-grid" },
				investmentChart({ kind: "area", title: "總資產變化", points: equityCurve, empty: "歷史估值不足——資料不足" }),
				investmentChart({ kind: "donut", title: "資產配置", slices: allocSlices, empty: "尚無配置資料" }),
				investmentChart({ kind: "donut", title: "產業配置", slices: arr(alloc.by_sector).map((s) => ({
					label: String(s.sector || s.label),
					value: num(s.value ?? s.weight)
				})), empty: "尚無產業資料" }),
				investmentChart({ kind: "donut", title: "幣別配置", slices: arr(alloc.by_currency).map((s) => ({
					label: String(s.currency || s.label),
					value: num(s.value ?? s.weight)
				})), empty: "尚無幣別資料" }),
				investmentChart({ kind: "bars", title: "投資現金流", bars: cashflow, empty: "尚無現金流資料" })),
			section({ title: "資料狀態", aside: dataFreshnessIndicator({ record: monitor.get().data }) },
				monitor.get().data
					? h("pre", { className: "inv-json" }, JSON.stringify(monitor.get().data, null, 2))
					: emptyState({ kind: "insufficient", detail: "監測狀態尚未送達。" })),
			h("p", { className: "inv-note" }, "不同來源的估值日期可能不同——各筆資料時間以標記為準，非同步即時資產。"));
	});
	const unsubs = queries.map((q) => q.subscribe(render));
	render();
	return {
		el,
		destroy() {
			unsubs.forEach((u) => u());
			queries.forEach((q) => q.destroy());
		}
	};
}
