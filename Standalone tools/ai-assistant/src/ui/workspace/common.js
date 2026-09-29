//! common.js — ai-assistant workspace view builders (React-free,
//! E180/C116).  Each helper returns a DOM node; ``DataTable`` keeps its
//! own paging store internally so the bounded-window contract survives
//! without React state.
import { createStore, h, rerender } from "../../../../../shared-layer/src/ui/toolWindow/dom.js";
import { fmt } from "./chart.js";

const arr = (v) => Array.isArray(v) ? v : [];

/** Honest empty states (§一): a page/section that lacks backend support
 * says so — it never fabricates data. */
export function emptyState(props = {}) {
	const label = props.kind === "not_integrated" ? "整合未完成" : props.kind === "insufficient" ? "資料不足" : "尚未提供";
	return h("div", { className: `inv-empty inv-empty-${props.kind || "no_data"}` },
		h("b", null, label),
		props.detail ? h("p", null, props.detail) : null);
}
export function section(props = {}, ...children) {
	return h("section", { className: "inv-section" },
		h("header", { className: "inv-section-head" },
			h("h3", null, props.title),
			props.aside ?? null),
		children);
}
export function metric(props = {}) {
	return h("div", { className: `inv-metric inv-metric-${props.tone || "flat"}` },
		h("span", { className: "inv-metric-label" }, props.label),
		h("b", { className: "inv-metric-value" }, props.value),
		props.sub ? h("span", { className: "inv-metric-sub" }, props.sub) : null);
}
export function badge(props = {}) {
	return h("span", { className: `inv-badge inv-badge-${props.tone || "info"}` }, props.text);
}
const num = (v) => {
	const n = Number(v);
	return Number.isFinite(n) ? n : 0;
};
export function money(props = {}) {
	const n = num(props.value);
	return h("span", { className: "inv-money" },
		props.currency ? `${props.currency} ` : "", fmt(n));
}
export function pnl(props = {}) {
	const n = num(props.value);
	return h("span", { className: n > 0 ? "pnl-up" : n < 0 ? "pnl-down" : "" },
		n > 0 ? "+" : "", fmt(n));
}
/**
 * Windowed table — renders at most ``pageSize`` rows at a time with
 * pager controls, so large histories never dump every row into the
 * DOM.  The pager owns its own page store; re-invoke ``dataTable`` on
 * each parent render (bounded, rows are re-sliced per page only).
 */
export function dataTable(props) {
	const size = props.pageSize ?? 25;
	const rows = arr(props.rows);
	const page = createStore(0);
	const wrap = h("div", { className: "inv-table-wrap" });
	if (!rows.length) return props.empty ?? emptyState();
	const render = () => {
		const pages = Math.max(1, Math.ceil(rows.length / size));
		const cur = Math.min(page.get(), pages - 1);
		const slice = rows.slice(cur * size, (cur + 1) * size);
		rerender(wrap, () => [
			h("table", { className: "inv-table" },
				h("thead", null, h("tr", null,
					props.columns.map((c) => h("th", null, c.label)))),
				h("tbody", null, slice.map((r) => h("tr", null,
					props.columns.map((c) => h("td", null,
						c.render ? c.render(r) : String(r[c.key] ?? "—"))))))),
			pages > 1 ? h("div", { className: "inv-pager" },
				h("button", {
					disabled: cur === 0,
					onClick: () => page.set(cur - 1)
				}, "上一頁"),
				h("span", null, `${cur + 1} / ${pages}（共 ${rows.length} 筆）`),
				h("button", {
					disabled: cur >= pages - 1,
					onClick: () => page.set(cur + 1)
				}, "下一頁")) : null
		]);
	};
	page.subscribe(render);
	render();
	return wrap;
}
