//! RecommendationPage.js — AI 買賣建議 (React-free, E180/C116).
import { createStore, h, rerender } from "../../../../../../shared-layer/src/ui/toolWindow/dom.js";
import { dataFreshnessIndicator } from "../freshness.js";
import { badge, emptyState } from "../common.js";

const arr = (v) => Array.isArray(v) ? v : [];
const FILTERS = [
	{ key: "all", label: "全部" },
	{ key: "tw", label: "台股" },
	{ key: "us", label: "美股" },
	{ key: "etf", label: "ETF" },
	{ key: "fund", label: "基金" },
	{ key: "realloc", label: "再配置" }
];
function marketOf(r) {
	const m = String(r.market || r.market_scope || "").toLowerCase();
	if (m) return m;
	const t = String(r.recommendation_type || "").toLowerCase();
	if (t.includes("realloc")) return "realloc";
	return "";
}

export function mount(ctx) {
	const local = createStore({ filter: "all", read: new Set() });
	const mon = ctx.query("investment_monitor_recommendations");
	const tw = ctx.query("investment_tw_recommendations");
	const us = ctx.query("investment_us_recommendations");
	const fund = ctx.query("investment_fund_recommendations");
	const queries = [mon, tw, us, fund];
	const submitOnly = (label, action, r) => {
		// § governance: ai-assistant cannot command investment-mobile.
		// Send through the honest control command — it will return
		// CONTROL_CHANNEL_UNAVAILABLE which we surface to the user.
		const ack = ctx.send("investment_autotrade_control", {
			action,
			strategy_id: r.strategy_id || "",
			instrument_id: r.instrument_id || r.fund_id || ""
		});
		if (!ack.ok) ctx.notify(`${label}未送出：${ack.message}`, "ERROR");
	};
	const markRead = (id) => {
		local.merge({ read: new Set(local.get().read).add(id) });
	};
	const el = h("div");
	const render = () => rerender(el, () => {
		const s = local.get();
		const all = [
			...arr(mon.get().data?.recommendations),
			...arr(tw.get().data?.recommendations).map((r) => ({ ...r, market: "tw" })),
			...arr(us.get().data?.recommendations).map((r) => ({ ...r, market: "us" })),
			...arr(fund.get().data?.recommendations).map((r) => ({ ...r, market: "fund" }))
		];
		const rows = all.filter((r) => {
			if (s.filter === "all") return true;
			const m = marketOf(r);
			if (s.filter === "etf") return String(r.asset_class || "").toLowerCase() === "etf";
			return m === s.filter;
		});
		return h("div", { className: "inv-page" },
			h("header", { className: "inv-page-head" },
				h("h2", null, "AI 買賣建議"),
				h("div", { className: "inv-filter" },
					FILTERS.map((f) => h("button", {
						className: s.filter === f.key ? "active" : "",
						onClick: () => local.merge({ filter: f.key })
					}, f.label)))),
			h("div", { className: "inv-cards" },
				rows.map((r, i) => {
					const id = String(r.id || r.recommendation_id || i);
					const isRead = s.read.has(id) || r.status === "READ";
					return h("article", { className: `inv-card${isRead ? " read" : ""}` },
						h("header", { className: "inv-card-head" },
							h("b", null, String(r.instrument_id || r.fund_id || "—")),
							badge({ text: String(r.recommendation_type || r.action || "—") }),
							isRead ? badge({ text: "已讀", tone: "muted" }) : null),
						h("p", { className: "inv-card-reason" }, String(r.reasoning || "—")),
						h("dl", { className: "inv-card-meta" },
							h("dt", null, "風險因素"), h("dd", null, String(r.risk_factors || r.risks || "—")),
							h("dt", null, "資料日期"), h("dd", null, String(r.data_date || r.generated_at || "—")),
							h("dt", null, "模型版本"), h("dd", null, String(r.model_version || "—")),
							h("dt", null, "策略版本"), h("dd", null, String(r.strategy_version || "—")),
							h("dt", null, "有效期間"), h("dd", null, String(r.valid_until || r.expires_at || "—"))),
						dataFreshnessIndicator({ record: r }),
						h("div", { className: "inv-card-actions" },
							h("button", { onClick: () => markRead(id) }, "標記已閱讀"),
							h("button", { onClick: () => submitOnly("SHADOW 研究", "start_shadow_research", r) }, "加入 SHADOW 研究"),
							h("button", { onClick: () => submitOnly("PAPER 方案", "create_paper_plan", r) }, "建立 PAPER 模擬方案")));
				}),
				!rows.length ? emptyState({ detail: "目前無符合篩選的建議。" }) : null),
			h("p", { className: "inv-note" }, "本階段不提供真實下單；SHADOW/PAPER 操作經治理通道提交，通道未接通時會明確提示。"));
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
