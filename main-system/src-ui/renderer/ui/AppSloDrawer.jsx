import { useEffect, useRef, useState } from "react";
import { Drawer } from "@/ui/drawer/Drawer";
import { mainSystemLocale } from "@/locales/main-system";
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
export function AppSloDrawer({ open, onClose }) {
	const [report, setReport] = useState(null);
	const [history, setHistory] = useState([]);
	const mountedRef = useRef(true);
	useEffect(() => {
		mountedRef.current = true;
		return () => {
			mountedRef.current = false;
		};
	}, []);
	useEffect(() => {
		if (!open) return;
		let cancelled = false;
		const fetchReport = async () => {
			const api = window.electron;
			if (!api?.invoke) return;
			try {
				const result = await api.invoke("app:get-perf-slo");
				if (cancelled || !mountedRef.current) return;
				setReport(result);
				setHistory((prev) => {
					if (!result.available) return prev;
					const last = prev[prev.length - 1];
					if (last && last.capturedAt === result.capturedAt) return prev;
					return [...prev, result].slice(-HISTORY_LIMIT);
				});
			} catch {
				if (!cancelled && mountedRef.current) {
					setReport({
						available: false,
						baselineVersion: null,
						capturedAt: null,
						snapshotAgeS: null,
						metrics: [],
						ipcPerCommand: [],
						error: "invoke-failed"
					});
				}
			}
		};
		void fetchReport();
		const timer = window.setInterval(() => {
			// hidden window → nobody can read the dashboard; defer the IPC poll
			if (document.visibilityState === "hidden") return;
			void fetchReport();
		}, POLL_INTERVAL_MS);
		return () => {
			cancelled = true;
			window.clearInterval(timer);
		};
	}, [open]);
	return <Drawer open={open} onClose={onClose} title={t.sloDashboard} eyebrow={t.systemOverview} icon="S">
      <div className="capacity-drawer">
        {report && !report.available && <article className="capacity-row" data-testid="slo-unavailable">
            <p className="capacity-row__detail">{t.sloUnavailable}</p>
          </article>}
        {report?.available && <>
            {report.metrics.filter((m) => m.key !== "ipc_samples").map((metric) => {
		const trend = metricTrend(history, metric.key);
		return <article key={metric.key} className="capacity-row" data-testid={`slo-${metric.key}`} data-status={metric.status}>
                    <div className="capacity-row__head">
                      <strong>
                        {METRIC_LABELS[metric.key] ?? metric.key}
                      </strong>
                      <span className="capacity-row__big">
                        {formatMetricValue(metric)}
                      </span>
                    </div>
                    <p className="capacity-row__detail">
                      <span className={`slo-status slo-status--${metric.status}`}>
                        {STATUS_LABELS[metric.status]}
                      </span>
                      {metric.budget !== null && <>
                          {" · "}
                          {t.sloBudget} {metric.budget}
                          {metric.unit === "MB" ? " MB" : metric.unit === "percent" ? "%" : " ms"}
                        </>}
                      {trend && <>
                          {" · "}
                          <span className={`slo-trend slo-trend--${trend}`} title={TREND_LABELS[trend]}>
                            {TREND_MARKS[trend]} {TREND_LABELS[trend]}
                          </span>
                        </>}
                      {report.snapshotAgeS !== null && <>
                          {" · "}
                          {t.sloSnapshotAge.replace("{seconds}", String(report.snapshotAgeS))}
                        </>}
                    </p>
                  </article>;
	})}
            {report.ipcPerCommand.length > 0 && <article className="capacity-row" data-testid="slo-per-command">
                <div className="capacity-row__head">
                  <strong>{t.sloPerCommand}</strong>
                </div>
                <dl className="slo-command-list">
                  {report.ipcPerCommand.slice(0, 12).map((row) => <div key={row.command} className="slo-command-row">
                      <dt>{row.command}</dt>
                      <dd>
                        {row.p95_ms !== null ? `${row.p95_ms.toFixed(1)} ms` : t.sloNoData}
                        {" · "}
                        {row.samples} samples
                      </dd>
                    </div>)}
                </dl>
              </article>}
          </>}
      </div>
    </Drawer>;
}
