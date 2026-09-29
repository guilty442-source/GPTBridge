//! RiskCenterPage.js — 風險中心 (React-free, E180/C116).
import { createStore, h, rerender } from "../../../../../../shared-layer/src/ui/toolWindow/dom.js";
import { dataFreshnessIndicator } from "../freshness.js";
import { badge, dataTable, emptyState, metric, section } from "../common.js";

const arr = (v) => Array.isArray(v) ? v : [];
const num = (v) => Number.isFinite(Number(v)) ? Number(v) : 0;
const CONFIRM_LABELS = {
	halt_new_orders: "停止新增訂單",
	cancel_orders: "取消模擬委託",
	liquidate_positions: "清算模擬持倉"
};

export function mount(ctx) {
	const local = createStore({ confirm: null });
	const risk = ctx.query("investment_monitor_risk");
	const events = ctx.query("investment_monitor_events");
	const ov = ctx.query("investment_autotrade_overview");
	const queries = [risk, events, ov];
	const unsubResult = ctx.onEvent("investment_autotrade_control_result", (p) => {
		const d = p;
		if (d?.ok === false) ctx.notify(`操作未執行：${d.error_code || d.message}`, "WARNING");
	});
	const control = (action) => {
		const ack = ctx.send("investment_autotrade_control", { action });
		if (!ack.ok) ctx.notify(`操作未送出：${ack.message}`, "ERROR");
		local.merge({ confirm: null });
	};
	const el = h("div");
	const render = () => rerender(el, () => {
		const s = local.get();
		const r = risk.get().data || {};
		return h("div", { className: "inv-page" },
			h("header", { className: "inv-page-head" },
				h("h2", null, "風險中心"),
				dataFreshnessIndicator({ record: r })),
			h("div", { className: "inv-metric-grid" },
				metric({ label: "持倉風險", value: String(r.position_risk ?? "—") }),
				metric({ label: "策略風險", value: String(r.strategy_risk ?? "—") }),
				metric({ label: "市場風險", value: String(r.market_risk ?? "—") }),
				metric({ label: "資金風險", value: String(r.capital_risk ?? "—") }),
				metric({ label: "集中度", value: num(r.concentration).toFixed(2) }),
				metric({ label: "最大回撤", value: `${num(r.max_drawdown).toFixed(2)}%` }),
				metric({ label: "系統異常", value: num(ov.get().data?.faults ?? r.faults) })),
			section({ title: "風控事件" },
				dataTable({
					rows: arr(events.get().data?.events || r.events),
					columns: [
						{ key: "triggered_at", label: "觸發時間", render: (x) => dataFreshnessIndicator({ record: x }) },
						{ key: "trigger", label: "觸發條件", render: (x) => String(x.trigger || x.reason || x.event_type || "—") },
						{ key: "strategy_id", label: "影響策略" },
						{ key: "account_id", label: "影響帳戶" },
						{ key: "outcome", label: "風控結果", render: (x) => String(x.outcome || x.decision || "—") },
						{ key: "status", label: "處理狀態", render: (x) => badge({ text: String(x.status || "—") }) }
					],
					empty: emptyState({ detail: "尚無風控事件。" })
				})),
			section({ title: "處置操作" },
				h("p", { className: "inv-note" }, "以下三者為不同語意的操作，分別送出："),
				h("div", { className: "inv-actions" },
					h("button", { className: "inv-warn", onClick: () => local.merge({ confirm: "halt_new_orders" }) }, "停止新增訂單"),
					h("button", { className: "inv-warn", onClick: () => local.merge({ confirm: "cancel_orders" }) }, "取消模擬委託"),
					h("button", { className: "inv-danger", onClick: () => local.merge({ confirm: "liquidate_positions" }) }, "清算模擬持倉"),
					h("button", { onClick: () => control("pause_strategy") }, "暫停策略"),
					h("button", { onClick: () => control("stop_all") }, "停止模擬操盤")),
				s.confirm ? h("div", { className: "inv-confirm" },
					h("p", null, `確認執行「${CONFIRM_LABELS[s.confirm]}」？此操作經治理通道送至投資引擎重新驗證。`),
					h("button", { className: "inv-danger", onClick: () => control(s.confirm) }, "確認"),
					h("button", { onClick: () => local.merge({ confirm: null }) }, "取消")) : null));
	});
	const unsubs = [local.subscribe(render), ...queries.map((q) => q.subscribe(render))];
	render();
	return {
		el,
		destroy() {
			unsubResult();
			unsubs.forEach((u) => u());
			queries.forEach((q) => q.destroy());
		}
	};
}
