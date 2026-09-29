//! MutualFundPage.js — 共同基金 (React-free, E180/C116).
import { createStore, h, rerender } from "../../../../../../shared-layer/src/ui/toolWindow/dom.js";
import { dataFreshnessIndicator } from "../freshness.js";
import { investmentChart } from "../chart.js";
import { badge, dataTable, emptyState, metric, money, pnl, section } from "../common.js";

const arr = (v) => Array.isArray(v) ? v : [];
const num = (v) => Number.isFinite(Number(v)) ? Number(v) : 0;
const FUND_ACTIONS = {
	SUBSCRIBE: "申購",
	ADD: "加碼",
	HOLD: "持有",
	REDUCE: "減碼",
	REDEEM: "贖回",
	SWITCH: "轉換"
};

export function mount(ctx) {
	const local = createStore({ fundId: "", sc: "A" });
	const portfolio = ctx.query("investment_fund_portfolio");
	const nav = ctx.query("investment_fund_nav", { fund_id: "", share_class_id: "A" });
	const recs = ctx.query("investment_fund_recommendations");
	const txns = ctx.query("investment_fund_transactions");
	const analysis = ctx.query("investment_fund_analysis", { fund_id: "" });
	const queries = [portfolio, nav, recs, txns, analysis];
	const refire = () => {
		const s = local.get();
		nav.setPayload({ fund_id: s.fundId, share_class_id: s.sc });
		analysis.setPayload({ fund_id: s.fundId });
	};
	const el = h("div");
	const render = () => rerender(el, () => {
		const s = local.get();
		const funds = arr(portfolio.get().data?.funds || portfolio.get().data?.positions);
		const navHist = arr(nav.get().data?.navs || nav.get().data?.history).map((n) => ({
			x: n.nav_date,
			y: num(n.nav)
		}));
		const latestNav = nav.get().data?.latest || nav.get().data?.nav;
		return h("div", { className: "inv-page" },
			h("header", { className: "inv-page-head" },
				h("h2", null, "共同基金"),
				h("span", { className: "inv-badge inv-badge-info" }, "已公告 NAV——非即時成交價")),
			section({ title: "基金清單", aside: dataFreshnessIndicator({ record: funds[0], source: "FILE_IMPORT" }) },
				dataTable({
					rows: funds,
					columns: [
						{ key: "fund_id", label: "基金" },
						{ key: "name", label: "名稱" },
						{ key: "isin", label: "ISIN" },
						{ key: "share_class_id", label: "級別" },
						{ key: "currency", label: "幣別" },
						{ key: "units", label: "持有單位" },
						{ key: "average_cost", label: "平均成本", render: (r) => money({ value: r.average_cost }) },
						{ key: "nav", label: "最新 NAV", render: (r) => money({ value: r.nav }) },
						{ key: "nav_date", label: "NAV 日期" },
						{ key: "market_value", label: "參考市值", render: (r) => money({ value: r.market_value }) },
						{ key: "total_return", label: "含息損益", render: (r) => pnl({ value: r.total_return }) }
					],
					empty: emptyState({ detail: "尚無基金持倉——請由資料匯入中心匯入。" })
				})),
			section({ title: "基金詳細資訊" },
				h("div", { className: "inv-search" },
					h("input", {
						value: s.fundId, placeholder: "基金代碼",
						dataset: { k: "fund-id" },
						onInput: (e) => { local.merge({ fundId: e.target.value }); refire(); }
					}),
					h("input", {
						value: s.sc, placeholder: "級別", style: { width: "64px" },
						dataset: { k: "fund-sc" },
						onInput: (e) => { local.merge({ sc: e.target.value }); refire(); }
					})),
				s.fundId ? [
					h("div", { className: "inv-metric-grid" },
						metric({ label: "最新已公告 NAV", value: money({ value: latestNav?.nav }), sub: `NAV 日期 ${latestNav?.nav_date || "—"}` }),
						metric({ label: "NAV 狀態", value: nav.get().data?.stale ? "已過期" : "有效", tone: nav.get().data?.stale ? "down" : "up" })),
					h("div", { className: "inv-chart-grid" },
						investmentChart({ kind: "line", title: "歷史淨值", points: navHist, empty: "無淨值歷史" })),
					h("div", { className: "inv-cards" },
						["績效", "配息", "費用", "持股", "產業配置", "國家配置", "公告"].map((k) => h("article", { className: "inv-card" },
							h("b", null, k),
							dataFreshnessIndicator({ record: (nav.get().data?.detail || {})[k] }),
							h("p", null, JSON.stringify((nav.get().data?.detail || {})[k] ?? "尚未提供")))))
				] : emptyState({ detail: "輸入基金代碼檢視詳細資訊。" })),
			section({ title: "星澄基金建議" },
				h("div", { className: "inv-cards" },
					arr(recs.get().data?.recommendations).map((r) => h("article", { className: "inv-card" },
						h("b", null, String(r.instrument_id || r.fund_id)),
						badge({ text: FUND_ACTIONS[String(r.recommendation_type)] || String(r.recommendation_type || "—") }),
						h("p", null, String(r.reasoning || "")),
						dataFreshnessIndicator({ record: r }))),
					!arr(recs.get().data?.recommendations).length ? emptyState({ detail: "尚無基金建議。" }) : null)),
			section({ title: "基金交易紀錄" },
				dataTable({
					rows: arr(txns.get().data?.transactions),
					columns: [
						{ key: "fund_id", label: "基金" },
						{ key: "type", label: "類型" },
						{ key: "units", label: "單位" },
						{ key: "amount", label: "金額", render: (r) => money({ value: r.amount }) },
						{ key: "nav_date", label: "NAV 日期" },
						{ key: "at", label: "時間" }
					],
					empty: emptyState({ detail: "尚無基金交易紀錄。" })
				})),
			h("p", { className: "inv-note" }, "基金申購/贖回不適用股票即時成交流程——本頁面不提供即時下單。"));
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
