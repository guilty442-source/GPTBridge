//! USEquityPage.js — 美國股票 (React-free, E180/C116).
import { createStore, h, rerender } from "../../../../../../shared-layer/src/ui/toolWindow/dom.js";
import { dataFreshnessIndicator } from "../freshness.js";
import { investmentChart } from "../chart.js";
import { dataTable, emptyState, metric, money, pnl, section } from "../common.js";

const arr = (v) => Array.isArray(v) ? v : [];
const num = (v) => Number.isFinite(Number(v)) ? Number(v) : 0;

export function mount(ctx) {
	const local = createStore({ query: "", iid: "" });
	const portfolio = ctx.query("investment_us_portfolio");
	const recs = ctx.query("investment_us_recommendations");
	const analysis = ctx.query("investment_us_analysis");
	const status = ctx.query("investment_market_status");
	const quote = ctx.query("investment_market_quote", { instrument_id: local.get().iid });
	const history = ctx.query("investment_market_history", {
		instrument_id: local.get().iid,
		timeframe: "1d",
		limit: 500
	});
	const queries = [portfolio, recs, analysis, status, quote, history];
	const submitQuery = () => {
		const iid = local.get().query.trim();
		local.merge({ iid });
		quote.setPayload({ instrument_id: iid });
		history.setPayload({ instrument_id: iid, timeframe: "1d", limit: 500 });
	};
	const el = h("div");
	const render = () => rerender(el, () => {
		const s = local.get();
		const positions = arr(portfolio.get().data?.positions || portfolio.get().data?.holdings);
		const us = status.get().data?.us || status.get().data?.markets?.us || {};
		const candles = arr(history.get().data?.candles).map((c) => ({
			x: c.candle_start || c.date,
			open: num(c.open),
			high: num(c.high),
			low: num(c.low),
			close: num(c.close),
			volume: num(c.volume)
		}));
		return h("div", { className: "inv-page" },
			h("header", { className: "inv-page-head" },
				h("h2", null, "美國股票"),
				h("div", { className: "inv-head-badges" },
					h("span", { className: "freshness freshness-delayed" },
						h("b", null, "帳戶來源"), h("i", null, "MANUAL / FILE_IMPORT")),
					h("span", { className: "inv-badge inv-badge-info" },
						`美股時段：${String(us.session || us.status || "—")}（依正式日曆/DST）`))),
			section({ title: "富邦美股複委託帳戶（離線）", aside: dataFreshnessIndicator({ record: positions[0], source: "FILE_IMPORT" }) },
				dataTable({
					rows: positions,
					columns: [
						{ key: "instrument_id", label: "商品" },
						{ key: "name", label: "名稱" },
						{ key: "quantity", label: "數量" },
						{ key: "average_cost", label: "美元成本", render: (r) => money({ value: r.average_cost, currency: "USD" }) },
						{ key: "market_value", label: "美元市值", render: (r) => money({ value: r.market_value, currency: "USD" }) },
						{ key: "market_value_twd", label: "台幣換算", render: (r) => money({ value: r.market_value_twd, currency: "TWD" }) },
						{ key: "unrealized_pnl", label: "美元損益", render: (r) => pnl({ value: r.unrealized_pnl }) },
						{ key: "distributions", label: "ETF 配息", render: (r) => money({ value: r.distributions, currency: "USD" }) }
					],
					empty: emptyState({ detail: "尚無美股持倉資料。" })
				})),
			section({ title: "行情與時段" },
				h("div", { className: "inv-metric-grid" },
					metric({ label: "市場狀態", value: String(us.status || us.session || "—"), sub: "依 TradingCalendar（America/New_York，含 DST）" }),
					metric({ label: "盤前", value: us.pre ? "開放" : "未開放/未授權" }),
					metric({ label: "盤後", value: us.post ? "開放" : "未開放/未授權" })),
				h("div", { className: "inv-search" },
					h("input", {
						value: s.query,
						placeholder: "輸入代碼（例：AAPL）",
						dataset: { k: "us-query" },
						onInput: (e) => local.merge({ query: e.target.value }),
						onKeydown: (e) => { if (e.key === "Enter") submitQuery(); }
					}),
					h("button", { onClick: submitQuery }, "查詢")),
				s.iid
					? h("div", { className: "inv-chart-grid" },
						investmentChart({ kind: "candles", title: `${s.iid} K 線`, candles, empty: "無 K 線歷史" }))
					: emptyState({ detail: "輸入代碼查詢行情。" })),
			section({ title: "星澄分析與建議" },
				h("div", { className: "inv-cards" },
					arr(recs.get().data?.recommendations).map((r) => h("article", { className: "inv-card" },
						h("b", null, String(r.instrument_id)),
						h("span", { className: "inv-badge inv-badge-info" }, String(r.recommendation_type || "—")),
						h("p", null, String(r.reasoning || "")),
						dataFreshnessIndicator({ record: r }))),
					!arr(recs.get().data?.recommendations).length ? emptyState({ detail: "尚無美股建議。" }) : null)),
			h("p", { className: "inv-note" }, "富邦複委託保持離線——無連線、無同步、無真實下單。"));
	});
	const unsubs = [local.subscribe(render), ...queries.map((q) => q.subscribe(render))];
	render();
	return {
		el,
		destroy() {
			unsubs.forEach((u) => u());
			queries.forEach((q) => q.destroy());
		}
	};
}
