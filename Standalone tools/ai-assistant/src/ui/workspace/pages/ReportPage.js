//! ReportPage.js — 投資報告中心 (React-free, E180/C116).
import { createStore, h, rerender } from "../../../../../../shared-layer/src/ui/toolWindow/dom.js";
import { dataFreshnessIndicator } from "../freshness.js";
import { badge, emptyState, section } from "../common.js";

const arr = (v) => Array.isArray(v) ? v : [];
const KINDS = [
	{ key: "", label: "全部" },
	{ key: "daily", label: "每日" },
	{ key: "weekly", label: "每週" },
	{ key: "monthly", label: "每月" },
	{ key: "strategy", label: "策略" },
	{ key: "fund", label: "基金分析" },
	{ key: "allocation", label: "資產配置" }
];

export function mount(ctx) {
	const local = createStore({ kind: "", q: "", open: null });
	const monitor = ctx.query("investment_monitor_reports");
	const autotrade = ctx.query("investment_autotrade_reports");
	const queries = [monitor, autotrade];
	const exportReport = (r, i) => {
		const blob = new Blob([JSON.stringify(r, null, 2)], { type: "application/json" });
		const a = document.createElement("a");
		a.href = URL.createObjectURL(blob);
		a.download = `report-${r.report_id || i}.json`;
		a.click();
		URL.revokeObjectURL(a.href);
	};
	const el = h("div");
	const render = () => rerender(el, () => {
		const s = local.get();
		const all = [...arr(monitor.get().data?.reports), ...arr(autotrade.get().data?.reports)];
		const rows = all.filter((r) => {
			if (s.kind && !String(r.report_type || r.kind || "").toLowerCase().includes(s.kind)) return false;
			if (s.q && !JSON.stringify(r).toLowerCase().includes(s.q.toLowerCase())) return false;
			return true;
		});
		return h("div", { className: "inv-page" },
			h("header", { className: "inv-page-head" },
				h("h2", null, "投資報告中心"),
				h("div", { className: "inv-filter" },
					KINDS.map((k) => h("button", {
						className: s.kind === k.key ? "active" : "",
						onClick: () => local.merge({ kind: k.key })
					}, k.label)),
					h("input", {
						value: s.q, placeholder: "搜尋…",
						dataset: { k: "report-search" },
						onInput: (e) => local.merge({ q: e.target.value })
					}))),
			h("div", { className: "inv-cards" },
				rows.map((r, i) => h("article", { className: "inv-card" },
					h("header", { className: "inv-card-head" },
						h("b", null, String(r.title || r.report_id || `報告 ${i + 1}`)),
						badge({ text: String(r.report_type || r.kind || "—") }),
						r.simulated === true ? badge({ text: "SIMULATED", tone: "warn" }) : null),
					h("dl", { className: "inv-card-meta" },
						h("dt", null, "資料來源"), h("dd", null, String(r.source || r.data_source || "—")),
						h("dt", null, "分析時間"), h("dd", null, String(r.generated_at || r.at || "—")),
						h("dt", null, "版本"), h("dd", null, String(r.version || "—"))),
					h("div", { className: "inv-card-actions" },
						h("button", { onClick: () => local.merge({ open: r }) }, "檢視"),
						h("button", { onClick: () => exportReport(r, i) }, "匯出")),
					dataFreshnessIndicator({ record: r }))),
				!rows.length ? emptyState({ detail: "尚無報告資料——報告由投資引擎排程產生後鏡像送達。" }) : null),
			s.open ? section({
				title: `報告內容：${String(s.open.title || "")}`,
				aside: h("button", { onClick: () => local.merge({ open: null }) }, "關閉")
			},
				h("pre", { className: "inv-json" }, JSON.stringify(s.open, null, 2)),
				h("p", { className: "inv-note" }, "標記 SIMULATED 之數值為模擬結果，非真實投資獲利。")) : null);
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
