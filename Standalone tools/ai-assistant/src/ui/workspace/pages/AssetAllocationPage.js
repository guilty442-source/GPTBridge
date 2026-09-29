//! AssetAllocationPage.js — 資產配置中心 (React-free, E180/C116).
import { h, rerender } from "../../../../../../shared-layer/src/ui/toolWindow/dom.js";
import { dataFreshnessIndicator } from "../freshness.js";
import { investmentChart } from "../chart.js";
import { dataTable, emptyState, section } from "../common.js";

const arr = (v) => Array.isArray(v) ? v : [];
const num = (v) => Number.isFinite(Number(v)) ? Number(v) : 0;
const DIMS = [
	{ key: "by_market", label: "市場配置" },
	{ key: "by_instrument", label: "商品配置" },
	{ key: "by_sector", label: "產業配置" },
	{ key: "by_country", label: "國家配置" },
	{ key: "by_currency", label: "幣別配置" },
	{ key: "cash", label: "現金配置" }
];

export function mount(ctx) {
	const alloc = ctx.query("investment_assets_allocation");
	const exposure = ctx.query("investment_assets_exposure");
	const rebalance = ctx.query("investment_monitor_recommendations");
	const queries = [alloc, exposure, rebalance];
	const el = h("div");
	const render = () => rerender(el, () => {
		const a = alloc.get().data?.allocation || alloc.get().data || {};
		const overlaps = arr(exposure.get().data?.overlaps || exposure.get().data?.shared_exposures);
		const candidates = arr(rebalance.get().data?.recommendations).filter((r) =>
			String(r.recommendation_type || "").toLowerCase().includes("realloc") ||
			String(r.category || "").toLowerCase().includes("rebal"));
		return h("div", { className: "inv-page" },
			h("header", { className: "inv-page-head" },
				h("h2", null, "資產配置中心"),
				dataFreshnessIndicator({ record: a })),
			h("div", { className: "inv-chart-grid" },
				DIMS.map((d) => investmentChart({
					kind: "donut", title: d.label,
					slices: arr(a[d.key]).map((r) => ({
						label: String(r.label || r.sector || r.country || r.currency || r.market || r.instrument_id || "—"),
						value: num(r.value ?? r.weight ?? r.market_value)
					})),
					empty: `${d.label}資料不足`
				}))),
			section({ title: "目標 vs 目前" },
				dataTable({
					rows: arr(a.target_vs_current),
					columns: [
						{ key: "bucket", label: "配置項" },
						{ key: "target", label: "目標" },
						{ key: "current", label: "目前" },
						{ key: "deviation", label: "偏差" }
					],
					empty: emptyState({ kind: "insufficient", detail: "尚未設定目標配置——整合未完成。" })
				})),
			section({ title: "再平衡候選方案" },
				h("div", { className: "inv-cards" },
					candidates.map((r) => h("article", { className: "inv-card" },
						h("b", null, String(r.title || r.instrument_id || "再配置")),
						h("p", null, String(r.reasoning || r.summary || "")),
						dataFreshnessIndicator({ record: r }))),
					!candidates.length ? emptyState({ detail: "目前無再平衡建議。" }) : null)),
			section({ title: "跨商品曝險（共同企業/產業辨識）" },
				dataTable({
					rows: overlaps,
					columns: [
						{ key: "entity", label: "共同曝險標的" },
						{ key: "instruments", label: "涉及商品", render: (r) => arr(r.instruments).join(", ") || "—" },
						{ key: "combined_weight", label: "合併權重" },
						{ key: "combined_value", label: "合併市值" }
					],
					empty: emptyState({ kind: "insufficient", detail: "曝險穿透資料送達後顯示（例：台積電/TSM ADR/半導體 ETF/科技基金）。" })
				}),
				h("p", { className: "inv-note" }, "ETF/基金穿透持股僅供曝險辨識，不重複加總至總資產。")));
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
