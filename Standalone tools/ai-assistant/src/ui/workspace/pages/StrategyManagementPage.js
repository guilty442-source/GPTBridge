//! StrategyManagementPage.js — 多策略管理 (React-free, E180/C116).
import { createStore, h, rerender } from "../../../../../../shared-layer/src/ui/toolWindow/dom.js";
import { badge, dataTable, emptyState, section } from "../common.js";

const arr = (v) => Array.isArray(v) ? v : [];
const num = (v) => Number.isFinite(Number(v)) ? Number(v) : 0;

export function mount(ctx) {
	const local = createStore({ detail: null });
	const strats = ctx.query("investment_autotrade_strategies");
	const defs = ctx.query("investment_strategy_list");
	const perf = ctx.query("investment_autotrade_performance");
	const queries = [strats, defs, perf];
	const unsubResult = ctx.onEvent("investment_autotrade_control_result", (p) => {
		const d = p;
		ctx.notify(d?.ok === false ? `操作未執行：${d.error_code || d.message}` : `操作已受理`, d?.ok === false ? "WARNING" : "INFO");
	});
	const control = (action, strategy_id = "", extra = {}) => {
		const ack = ctx.send("investment_autotrade_control", { action, strategy_id, ...extra });
		if (!ack.ok) ctx.notify(`操作未送出：${ack.message}`, "ERROR");
	};
	const el = h("div");
	const render = () => rerender(el, () => {
		const s = local.get();
		const runtimes = arr(strats.get().data?.strategies);
		const perfById = Object.fromEntries(arr(perf.get().data?.strategies || perf.get().data?.performance).map((p) => [String(p.strategy_id), p]));
		return h("div", { className: "inv-page" },
			h("header", { className: "inv-page-head" },
				h("h2", null, "多策略管理"),
				h("div", { className: "inv-actions" },
					h("button", { onClick: () => control("create_draft") }, "新增策略草稿"))),
			section({ title: "策略運行" },
				dataTable({
					rows: runtimes,
					columns: [
						{ key: "strategy_id", label: "名稱" },
						{ key: "version", label: "版本" },
						{ key: "market_scope", label: "適用市場" },
						{ key: "instrument_scope", label: "監測商品", render: (r) => arr(r.instrument_scope).join(", ") || "—" },
						{ key: "execution_mode", label: "運行模式", render: (r) => badge({ text: String(r.execution_mode || "—") }) },
						{ key: "capital", label: "分配資金" },
						{ key: "capital_used", label: "資金使用率" },
						{
							key: "state", label: "運行狀態",
							render: (r) => badge({ text: String(r.state || "—"), tone: r.state === "RUNNING" ? "ok" : r.state === "RISK_HALTED" ? "danger" : "info" })
						},
						{
							key: "pnl", label: "策略損益",
							render: (r) => {
								const p = perfById[String(r.strategy_id)];
								return p ? h("span", null, num(p.total_pnl).toFixed(2)) : "—";
							}
						},
						{
							key: "dd", label: "最大回撤",
							render: (r) => {
								const p = perfById[String(r.strategy_id)];
								return p ? `${num(p.max_drawdown).toFixed(2)}%` : "—";
							}
						},
						{
							key: "ops", label: "操作",
							render: (r) => h("span", { className: "inv-row-actions" },
								h("button", { onClick: () => local.merge({ detail: r }) }, "檢視"),
								h("button", { onClick: () => control("start", String(r.strategy_id)) }, "啟動模擬"),
								h("button", { onClick: () => control("pause", String(r.strategy_id)) }, "暫停"),
								h("button", { onClick: () => control("stop", String(r.strategy_id)) }, "停止"))
						}
					],
					empty: emptyState({ detail: "尚無策略運行資料。" })
				})),
			s.detail ? section({
				title: `策略詳細：${String(s.detail.strategy_id)}`,
				aside: h("button", { onClick: () => local.merge({ detail: null }) }, "關閉")
			},
				h("pre", { className: "inv-json" }, JSON.stringify(s.detail, null, 2)),
				h("p", { className: "inv-note" }, "RUNNING 策略的正式版本不可由此修改——修改請建立新版本草稿（草案參數僅在 DRAFT 可編輯）。"),
				h("button", { onClick: () => control("edit_draft", String(s.detail.strategy_id)) }, "修改草稿參數（建立新版本）")) : null,
			section({ title: "策略定義（鏡像）" },
				dataTable({
					rows: arr(defs.get().data?.strategies),
					columns: [
						{ key: "strategy_id", label: "策略" },
						{ key: "strategy_type", label: "型別" },
						{ key: "version", label: "版本" },
						{ key: "status", label: "狀態" },
						{ key: "market_scope", label: "市場" },
						{ key: "created_at", label: "建立時間" }
					],
					empty: emptyState({ detail: "尚無策略定義鏡像。" })
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
