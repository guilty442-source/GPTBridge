//! TaiwanEquityPage.js — 台灣股票 (React-free, E180/C116).
import { createStore, h, rerender } from "../../../../../../shared-layer/src/ui/toolWindow/dom.js";
import { dataFreshnessIndicator } from "../freshness.js";
import { investmentChart } from "../chart.js";
import { dataTable, emptyState, metric, money, pnl, section } from "../common.js";

const arr = (v) => Array.isArray(v) ? v : [];
const num = (v) => Number.isFinite(Number(v)) ? Number(v) : 0;

export function mount(ctx) {
	const local = createStore({ query: "", iid: ctx.ui.get().instrumentId || "" });
	const portfolio = ctx.query("investment_tw_portfolio");
	const recs = ctx.query("investment_tw_recommendations");
	const analysis = ctx.query("investment_tw_analysis");
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
		const candles = arr(history.get().data?.candles).map((c) => ({
			x: c.candle_start || c.date,
			open: num(c.open),
			high: num(c.high),
			low: num(c.low),
			close: num(c.close),
			volume: num(c.volume)
		}));
		const trend = candles.map((c) => ({ x: c.x, y: c.close }));
		return h("div", { className: "inv-page" },
			h("header", { className: "inv-page-head" },
				h("h2", null, "台灣股票"),
				h("div", { className: "inv-head-badges" },
					h("span", { className: "freshness freshness-delayed" },
						h("b", null, "帳戶來源"), h("i", null, "MANUAL / FILE_IMPORT / PAPER")),
					dataFreshnessIndicator({ record: status.get().data }))),
			section({ title: "國泰台股帳戶（離線）", aside: dataFreshnessIndicator({ record: positions[0], source: "FILE_IMPORT" }) },
				dataTable({
					rows: positions,
					columns: [
						{ key: "instrument_id", label: "商品" },
						{ key: "name", label: "名稱" },
						{ key: "quantity", label: "數量" },
						{ key: "average_cost", label: "平均成本", render: (r) => money({ value: r.average_cost }) },
						{ key: "market_value", label: "參考市值", render: (r) => money({ value: r.market_value }) },
						{ key: "unrealized_pnl", label: "未實現損益", render: (r) => pnl({ value: r.unrealized_pnl }) },
						{ key: "dividends", label: "累計股息", render: (r) => money({ value: r.dividends }) },
						{ key: "source", label: "來源", render: (r) => h("span", { className: "inv-src" }, String(r.source || "MANUAL")) }
					],
					empty: emptyState({ detail: "尚無台股持倉資料——請由資料匯入中心匯入。" })
				})),
			section({ title: "行情" },
				h("div", { className: "inv-search" },
					h("input", {
						value: s.query,
						placeholder: "輸入股票代碼（例：2330）",
						dataset: { k: "tw-query" },
						onInput: (e) => local.merge({ query: e.target.value }),
						onKeydown: (e) => { if (e.key === "Enter") submitQuery(); }
					}),
					h("button", { onClick: submitQuery }, "查詢")),
				s.iid ? [
					h("div", { className: "inv-metric-grid" },
						metric({
							label: "最新價",
							value: num(quote.get().data?.quote?.price ?? quote.get().data?.quote?.close) || "—",
							sub: dataFreshnessIndicator({ record: quote.get().data?.quote })
						}),
						metric({ label: "成交量", value: num(quote.get().data?.quote?.volume) || "—" })),
					h("div", { className: "inv-chart-grid" },
						investmentChart({ kind: "candles", title: `${s.iid} K 線`, candles, empty: "無 K 線歷史" }),
						investmentChart({ kind: "line", title: "收盤趨勢", points: trend, empty: "無歷史" }))
				] : emptyState({ detail: "輸入代碼查詢行情。" })),
			section({ title: "星澄分析與建議" },
				h("div", { className: "inv-cards" },
					arr(recs.get().data?.recommendations).map((r) => h("article", { className: "inv-card" },
						h("b", null, String(r.instrument_id)),
						h("span", { className: `inv-badge inv-badge-${String(r.recommendation_type || "").toLowerCase()}` },
							String(r.recommendation_type || "—")),
						h("p", null, String(r.reasoning || "")),
						dataFreshnessIndicator({ record: r }))),
					!arr(recs.get().data?.recommendations).length ? emptyState({ detail: "尚無台股建議。" }) : null),
				analysis.get().data
					? h("pre", { className: "inv-json" }, JSON.stringify(analysis.get().data, null, 2))
					: emptyState({ kind: "insufficient", detail: "分析結果尚未產生。" })));
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
