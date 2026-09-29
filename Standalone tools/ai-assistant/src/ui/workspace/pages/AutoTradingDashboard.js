//! AutoTradingDashboard.js — 自動操盤控制台 (React-free, E180/C116).
import { createStore, h, rerender } from "../../../../../../shared-layer/src/ui/toolWindow/dom.js";
import { dataFreshnessIndicator, simulatedBadge } from "../freshness.js";
import { badge, dataTable, emptyState, metric, pnl, section } from "../common.js";

const arr = (v) => Array.isArray(v) ? v : [];
const num = (v) => Number.isFinite(Number(v)) ? Number(v) : 0;

export function mount(ctx) {
	const local = createStore({ ctrlResult: "" });
	const overview = ctx.query("investment_autotrade_overview");
	const strats = ctx.query("investment_autotrade_strategies");
	const perf = ctx.query("investment_autotrade_performance");
	const sim = ctx.query("investment_sim_status");
	const execs = ctx.query("investment_sim_executions");
	const mode = ctx.query("investment_trade_mode");
	const queries = [overview, strats, perf, sim, execs, mode];
	// react to control results + mirror events
	const unsubResult = ctx.onEvent("investment_autotrade_control_result", (p) => {
		const d = p;
		local.merge({ ctrlResult: String(d?.error_code || d?.message || "已送出") });
		if (d?.ok === false) ctx.notify(`控制未執行：${d.error_code}`, "WARNING");
	});
	const control = (action, strategy_id = "") => {
		const ack = ctx.send("investment_autotrade_control", { action, strategy_id });
		if (!ack.ok) ctx.notify(`控制未送出：${ack.message}`, "ERROR");
	};
	const el = h("div");
	const render = () => rerender(el, () => {
		const s = local.get();
		const ov = overview.get().data || {};
		const strategies = arr(strats.get().data?.strategies);
		return h("div", { className: "inv-page" },
			h("header", { className: "inv-page-head" },
				h("h2", null, "自動操盤控制台"),
				simulatedBadge()),
			h("div", { className: "inv-metric-grid" },
				metric({ label: "運行模式", value: String(mode.get().data?.mode || ov.mode || "ANALYSIS"), sub: "LIVE 未啟用" }),
				metric({ label: "星澄模型", value: ov.model_available === false ? "不可用（降級安全）" : "可用", tone: ov.model_available === false ? "down" : "up" }),
				metric({ label: "行情資料", value: String(ov.data_state || ov.data_quality || "—"), sub: "過期即阻擋下單" }),
				metric({ label: "運行策略", value: `${num(ov.running)} / ${num(ov.total ?? strategies.length)}` }),
				metric({ label: "模擬帳戶數", value: num(sim.get().data?.accounts) }),
				metric({ label: "SHADOW/PAPER", value: "SHADOW / PAPER", sub: String(ov.disclaimer || "LIVE stays phase-locked") })),
			section({
				title: "策略運行狀態",
				aside: h("div", { className: "inv-actions" },
					h("button", { onClick: () => control("start") }, "開始策略"),
					h("button", { onClick: () => control("pause") }, "暫停策略"),
					h("button", { onClick: () => control("stop") }, "停止策略"),
					h("button", { onClick: () => control("set_capital") }, "設定模擬資金"))
			},
				s.ctrlResult ? h("p", { className: "inv-note inv-ctrl-result" }, `控制結果：${s.ctrlResult}`) : null,
				dataTable({
					rows: strategies,
					columns: [
						{ key: "strategy_id", label: "策略" },
						{ key: "version", label: "版本" },
						{ key: "execution_mode", label: "模式", render: (r) => badge({ text: String(r.execution_mode || "—") }) },
						{ key: "state", label: "狀態" },
						{ key: "market_scope", label: "市場" },
						{
							key: "action", label: "操作",
							render: (r) => h("span", { className: "inv-row-actions" },
								h("button", { onClick: () => control("start", String(r.strategy_id)) }, "啟動"),
								h("button", { onClick: () => control("pause", String(r.strategy_id)) }, "暫停"),
								h("button", { onClick: () => control("stop", String(r.strategy_id)) }, "停止"))
						}
					],
					empty: emptyState({ detail: "尚無策略鏡像資料。" })
				}),
				h("p", { className: "inv-note" }, "控制經治理通道提交至投資引擎；通道未接通時顯示 CONTROL_CHANNEL_UNAVAILABLE，不做假成功。")),
			section({ title: "策略績效（模擬）" },
				h("div", { className: "inv-cards" },
					arr(perf.get().data?.strategies || perf.get().data?.performance).map((p) => h("article", { className: "inv-card" },
						h("b", null, String(p.strategy_id)),
						h("dl", { className: "inv-card-meta" },
							h("dt", null, "總損益"), h("dd", null, pnl({ value: p.total_pnl })),
							h("dt", null, "勝率"), h("dd", null, `${num(p.win_rate).toFixed(1)}%`),
							h("dt", null, "最大回撤"), h("dd", null, `${num(p.max_drawdown).toFixed(2)}%`),
							h("dt", null, "成交數"), h("dd", null, num(p.fills))),
						dataFreshnessIndicator({ record: p }))),
					!arr(perf.get().data?.strategies || perf.get().data?.performance).length
						? emptyState({ detail: "尚無模擬績效資料。" }) : null)),
			section({ title: "模擬成交" },
				dataTable({
					rows: arr(execs.get().data?.executions),
					columns: [
						{ key: "strategy_id", label: "策略" },
						{ key: "instrument_id", label: "商品" },
						{ key: "side", label: "方向" },
						{ key: "quantity", label: "數量" },
						{ key: "price", label: "價格" },
						{ key: "status", label: "狀態" },
						{ key: "at", label: "時間" }
					],
					empty: emptyState({ detail: "尚無模擬成交。" })
				})));
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
