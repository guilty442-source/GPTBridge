import { createDrawer } from "./Drawer.js";
import { mainSystemLocale } from "@/locales/main-system";
import { h } from "@/shared/mini/dom.js";
const t = mainSystemLocale.product;
const POLL_INTERVAL_MS = 3e4;
const HISTORY_LIMIT = 40;
const METRIC_LABELS = {
	ipc_p95_ms: t.sloIpcP95,
	ipc_p50_ms: t.sloIpcP50,
	ipc_p99_ms: t.sloIpcP99,
	backend_rss_mb: t.sloBackendRss,
	cpu_percent: t.sloCpu,
	gpu_vram_used_mb: t.sloGpuVram,
	gpu_utilization: t.sloGpuUtil,
	ipc_samples: t.sloIpcSamples
};
const STATUS_LABELS = {
	ok: t.sloWithinBudget,
	warn: t.sloNearBudget,
	over: t.sloOverBudget,
	measured: t.sloMeasured,
	unknown: t.sloNoData
};
function formatMetricValue(metric) {
	if (metric.value === null) return t.sloNoData;
	const rounded = metric.unit === "count" ? Math.round(metric.value).toString() : metric.value.toFixed(1);
	const suffix = {
		ms: " ms",
		MB: " MB",
		percent: "%",
		count: ""
	}[metric.unit];
	return `${rounded}${suffix}`;
}
function metricTrend(history, key) {
	if (history.length < 2) return null;
	const find = (report) => report.metrics.find((m) => m.key === key)?.value ?? null;
	const latest = find(history[history.length - 1]);
	const previous = find(history[history.length - 2]);
	if (latest === null || previous === null || latest === previous) {
		return latest === null || previous === null ? null : "flat";
	}
	return latest > previous ? "up" : "down";
}
const TREND_LABELS = {
	up: t.sloTrendUp,
	down: t.sloTrendDown,
	flat: t.sloTrendFlat
};
const TREND_MARKS = {
	up: "▲",
	down: "▼",
	flat: "—"
};

/**
* createSloDrawer — former AppSloDrawer.jsx.
* Polls `app:get-perf-slo` via window.electron while open (30 s cadence,
* visibility-gated — the same idle-gating contract the JSX version had);
* rebuilds the body on each new report.
* Returns {setOpen(open), destroy()}.
*/
export function createSloDrawer({ onClose }) {
	let report = null;
	let history = [];
	let pollTimer = null;
	let open = false;
	let destroyed = false;

	const rebuild = () => {
		const body = drawer.body();
		if (!body) return;
		body.replaceChildren();
		const wrap = h("div", { className: "capacity-drawer" });
		if (report && !report.available) {
			wrap.appendChild(h("article", { className: "capacity-row", dataset: { testid: "slo-unavailable" } },
				h("p", { className: "capacity-row__detail" }, t.sloUnavailable)));
		}
		if (report?.available) {
			for (const metric of report.metrics.filter((m) => m.key !== "ipc_samples")) {
				const trend = metricTrend(history, metric.key);
				wrap.appendChild(h("article", {
					className: "capacity-row",
					dataset: { testid: `slo-${metric.key}`, status: metric.status }
				},
					h("div", { className: "capacity-row__head" },
						h("strong", null, METRIC_LABELS[metric.key] ?? metric.key),
						h("span", { className: "capacity-row__big" }, formatMetricValue(metric))),
					h("p", { className: "capacity-row__detail" },
						h("span", { className: `slo-status slo-status--${metric.status}` }, STATUS_LABELS[metric.status]),
						metric.budget !== null ? ` · ${t.sloBudget} ${metric.budget}${metric.unit === "MB" ? " MB" : metric.unit === "percent" ? "%" : " ms"}` : "",
						trend ? h("span", null,
							" · ",
							h("span", { className: `slo-trend slo-trend--${trend}`, title: TREND_LABELS[trend] },
								`${TREND_MARKS[trend]} ${TREND_LABELS[trend]}`)) : null,
						report.snapshotAgeS !== null ? ` · ${t.sloSnapshotAge.replace("{seconds}", String(report.snapshotAgeS))}` : "")));
			}
			if (report.ipcPerCommand.length > 0) {
				wrap.appendChild(h("article", { className: "capacity-row", dataset: { testid: "slo-per-command" } },
					h("div", { className: "capacity-row__head" },
						h("strong", null, t.sloPerCommand)),
					h("dl", { className: "slo-command-list" },
						report.ipcPerCommand.slice(0, 12).map((row) => h("div", { className: "slo-command-row" },
							h("dt", null, row.command),
							h("dd", null,
								row.p95_ms !== null ? `${row.p95_ms.toFixed(1)} ms` : t.sloNoData,
								" · ",
								`${row.samples} samples`))))));
			}
		}
		body.appendChild(wrap);
	};

	const fetchReport = async () => {
		const api = window.electron;
		if (!api?.invoke) return;
		try {
			const result = await api.invoke("app:get-perf-slo");
			if (!open || destroyed) return;
			report = result;
			if (result.available) {
				const last = history[history.length - 1];
				if (!last || last.capturedAt !== result.capturedAt) {
					history = [...history, result].slice(-HISTORY_LIMIT);
				}
			}
		} catch {
			if (open && !destroyed) {
				report = {
					available: false,
					baselineVersion: null,
					capturedAt: null,
					snapshotAgeS: null,
					metrics: [],
					ipcPerCommand: [],
					error: "invoke-failed"
				};
			}
		}
		rebuild();
	};

	const startPolling = () => {
		stopPolling();
		void fetchReport();
		pollTimer = window.setInterval(() => {
			// hidden window → nobody can read the dashboard; defer the IPC poll
			if (document.visibilityState === "hidden") return;
			void fetchReport();
		}, POLL_INTERVAL_MS);
	};
	const stopPolling = () => {
		if (pollTimer !== null) {
			window.clearInterval(pollTimer);
			pollTimer = null;
		}
	};

	const drawer = createDrawer({
		onClose,
		title: t.sloDashboard,
		eyebrow: t.systemOverview,
		icon: "S",
		render: rebuild
	});
	return {
		setOpen: (next) => {
			open = next;
			drawer.setOpen(next);
			if (next) startPolling(); else stopPolling();
		},
		destroy: () => {
			destroyed = true;
			stopPolling();
			drawer.destroy();
		}
	};
}
export { createSloDrawer as default };
