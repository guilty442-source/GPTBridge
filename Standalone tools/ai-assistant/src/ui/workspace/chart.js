//! chart.js — ai-assistant workspace SVG charts (React-free, E180/C116).
//! Same bounded-render contract as the retired ``InvestmentChart``:
//! ``downsample``/``downsampleCandles`` cap points before they reach the
//! DOM; ``investmentChart`` builds the figure node once per call.
import { h } from "../../../../../shared-layer/src/ui/toolWindow/dom.js";

/** Bucket-average downsampling — the renderer never receives the full
 * tick history; at most `max` points reach the DOM. */
export function downsample(rows, max = 240) {
	if (rows.length <= max) return rows;
	const out = [];
	const bucket = rows.length / max;
	for (let i = 0; i < max; i++) {
		const slice = rows.slice(Math.floor(i * bucket), Math.floor((i + 1) * bucket));
		if (!slice.length) continue;
		const y = slice.reduce((s, r) => s + r.y, 0) / slice.length;
		out.push({ ...slice[slice.length - 1], y });
	}
	return out;
}
export function downsampleCandles(rows, max = 160) {
	if (rows.length <= max) return rows;
	const out = [];
	const bucket = rows.length / max;
	for (let i = 0; i < max; i++) {
		const slice = rows.slice(Math.floor(i * bucket), Math.floor((i + 1) * bucket));
		if (!slice.length) continue;
		out.push({
			x: slice[slice.length - 1].x,
			open: slice[0].open,
			high: Math.max(...slice.map((c) => c.high)),
			low: Math.min(...slice.map((c) => c.low)),
			close: slice[slice.length - 1].close,
			volume: slice.reduce((s, c) => s + (c.volume || 0), 0)
		});
	}
	return out;
}
const W = 640;
const SVG = "http://www.w3.org/2000/svg";
function snode(tag, attrs = {}, ...children) {
	const el = document.createElementNS(SVG, tag);
	for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, String(v));
	for (const c of children.flat(Infinity)) {
		if (c == null) continue;
		el.append(c instanceof Node ? c : String(c));
	}
	return el;
}
function renderChart(props, ht) {
	if (props.kind === "line" || props.kind === "area") {
		const pts = downsample((props.points || []).filter((p) => Number.isFinite(p.y)));
		if (pts.length < 2) return null;
		const ys = pts.map((p) => p.y);
		const lo = Math.min(...ys);
		const hi = Math.max(...ys);
		const span = hi - lo || 1;
		const step = W / (pts.length - 1);
		const xy = pts.map((p, i) => `${(i * step).toFixed(1)},${(ht - 14 - (p.y - lo) / span * (ht - 28)).toFixed(1)}`);
		const path = `M${xy.join(" L")}`;
		return snode("svg", { viewBox: `0 0 ${W} ${ht}`, preserveAspectRatio: "none", class: "inv-svg" },
			props.kind === "area" ? snode("path", { d: `${path} L${W},${ht - 14} L0,${ht - 14} Z`, class: "inv-chart-area" }) : null,
			snode("path", { d: path, class: "inv-chart-line", fill: "none" }),
			snode("text", { x: 4, y: 14, class: "inv-chart-tick" }, fmt(hi)),
			snode("text", { x: 4, y: ht - 4, class: "inv-chart-tick" }, fmt(lo)));
	}
	if (props.kind === "candles") {
		const cs = downsampleCandles(props.candles || []);
		if (cs.length < 2) return null;
		const lo = Math.min(...cs.map((c) => c.low));
		const hi = Math.max(...cs.map((c) => c.high));
		const span = hi - lo || 1;
		const bw = Math.max(1, W / cs.length * .6);
		const Y = (v) => ht - 14 - (v - lo) / span * (ht - 28);
		return snode("svg", { viewBox: `0 0 ${W} ${ht}`, preserveAspectRatio: "none", class: "inv-svg" },
			cs.map((c, i) => {
				const x = (i + .5) * (W / cs.length);
				const up = c.close >= c.open;
				return snode("g", { class: up ? "c-up" : "c-down" },
					snode("line", { x1: x, x2: x, y1: Y(c.high), y2: Y(c.low), strokeWidth: 1 }),
					snode("rect", {
						x: x - bw / 2,
						y: Math.min(Y(c.open), Y(c.close)),
						width: bw,
						height: Math.max(1, Math.abs(Y(c.open) - Y(c.close)))
					}));
			}));
	}
	if (props.kind === "donut") {
		const slices = props.slices || [];
		const total = slices.reduce((s, x) => s + Math.max(0, x.value), 0);
		if (total <= 0) return null;
		const r = ht / 2 - 12;
		const cx = 100;
		const cy = ht / 2;
		let angle = -Math.PI / 2;
		const arcs = slices.filter((s) => s.value > 0).map((s, i) => {
			const a = s.value / total * Math.PI * 2;
			const x1 = cx + r * Math.cos(angle);
			const y1 = cy + r * Math.sin(angle);
			angle += a;
			const x2 = cx + r * Math.cos(angle);
			const y2 = cy + r * Math.sin(angle);
			const large = a > Math.PI ? 1 : 0;
			return snode("path", {
				d: `M${cx},${cy} L${x1},${y1} A${r},${r} 0 ${large} 1 ${x2},${y2} Z`,
				class: `donut-slice donut-${i % 8}`
			});
		});
		return h("div", { className: "inv-donut-wrap" },
			snode("svg", { viewBox: `0 0 200 ${ht}`, class: "inv-svg inv-donut" },
				arcs,
				snode("circle", { cx, cy, r: r * .55, class: "donut-hole" })),
			h("ul", { className: "inv-donut-legend" },
				slices.map((s, i) => h("li", null,
					h("i", { className: `dot donut-${i % 8}` }),
					`${s.label} — ${(s.value / total * 100).toFixed(1)}%`))));
	}
	if (props.kind !== "bars") return null;
	const bars = props.bars || [];
	if (!bars.length) return null;
	const max = Math.max(...bars.map((b) => Math.abs(b.value)), 0) || 1;
	return h("div", { className: "inv-bars", style: { minHeight: `${ht}px` } },
		bars.map((b) => h("div", { className: "inv-bar-row" },
			h("span", { className: "inv-bar-label" }, b.label),
			h("div", { className: "inv-bar-track" },
				h("div", {
					className: `inv-bar-fill${b.value < 0 ? " neg" : ""}`,
					style: { width: `${Math.min(100, Math.abs(b.value) / max * 100)}%` }
				})),
			h("span", { className: "inv-bar-val" }, fmt(b.value)))));
}
/** Shared SVG chart — rebuilt on demand by the owning page render. */
export function investmentChart(props) {
	const ht = props.height ?? 180;
	const body = renderChart(props, ht);
	return h("figure", { className: "inv-chart" },
		props.title ? h("figcaption", null, props.title) : null,
		body ?? h("div", { className: "inv-empty" }, props.empty || "資料不足"));
}
export function fmt(v) {
	if (!Number.isFinite(v)) return "—";
	const a = Math.abs(v);
	if (a >= 1e8) return `${(v / 1e8).toFixed(2)}億`;
	if (a >= 1e4) return `${(v / 1e4).toFixed(1)}萬`;
	return v.toLocaleString("zh-TW", { maximumFractionDigits: 2 });
}
