//! StrategyLaboratoryPage.js — 策略實驗室 (React-free, E180/C116).
import { createStore, h, rerender } from "../../../../../../shared-layer/src/ui/toolWindow/dom.js";
import { investmentChart } from "../chart.js";
import { badge, dataTable, emptyState, metric, pnl, section } from "../common.js";

const arr = (v) => Array.isArray(v) ? v : [];
const num = (v) => Number.isFinite(Number(v)) ? Number(v) : 0;

export function mount(ctx) {
	const local = createStore({
		sel: null,
		form: {
			strategy_id: "",
			instrument_id: "",
			from: "",
			to: "",
			initial_capital: "100000",
			fee: "",
			slippage: ""
		}
	});
	const strats = ctx.query("investment_strategy_list");
	const results = ctx.query("investment_backtest_results");
	const queries = [strats, results];
	const unsubResult = ctx.onEvent("investment_autotrade_control_result", (p) => {
		const d = p;
		if (d?.ok === false) ctx.notify(`回測未執行：${d.error_code || d.message}`, "WARNING");
	});
	const setForm = (patch) => local.merge({ form: { ...local.get().form, ...patch } });
	const launch = () => {
		const form = local.get().form;
		const ack = ctx.send("investment_autotrade_control", {
			action: "run_backtest",
			...form,
			initial_capital: Number(form.initial_capital) || 0
		});
		if (!ack.ok) ctx.notify(`回測未送出：${ack.message}`, "ERROR");
	};
	const field = (label, key, attrs = {}) => h("label", null, label,
		h("input", {
			value: local.get().form[key],
			dataset: { k: `lab-${key}` },
			onInput: (e) => setForm({ [key]: e.target.value }),
			...attrs
		}));
	const el = h("div");
	const render = () => rerender(el, () => {
		const s = local.get();
		const rows = arr(results.get().data?.results);
		const detail = s.sel || rows[0];
		const curve = arr(detail?.equity_curve || detail?.curve).map((p, i) => ({
			x: p.date || i,
			y: num(p.value ?? p.equity)
		}));
		return h("div", { className: "inv-page" },
			h("header", { className: "inv-page-head" },
				h("h2", null, "策略實驗室"),
				badge({ text: "回測/驗證結果" })),
			section({ title: "回測設定" },
				h("div", { className: "inv-form" },
					h("label", null, "策略",
						h("select", {
							dataset: { k: "lab-strategy" },
							onChange: (e) => setForm({ strategy_id: e.target.value })
						},
							h("option", { value: "", selected: !s.form.strategy_id }, "選擇策略"),
							arr(strats.get().data?.strategies).map((st) => h("option", {
								value: String(st.strategy_id),
								selected: String(st.strategy_id) === s.form.strategy_id
							}, `${String(st.strategy_id)} v${String(st.version ?? "")}`)))),
					field("商品", "instrument_id"),
					field("起始日", "from", { type: "date" }),
					field("結束日", "to", { type: "date" }),
					field("初始資金", "initial_capital"),
					field("交易費用", "fee", { placeholder: "預設費率" }),
					field("滑價假設", "slippage", { placeholder: "預設" }),
					h("button", { className: "inv-primary", onClick: launch }, "啟動回測")),
				h("p", { className: "inv-note" }, "回測於投資引擎端執行——此介面送出治理請求；通道未接通時顯示錯誤，不假造結果。")),
			section({ title: "回測結果" },
				dataTable({
					rows,
					columns: [
						{ key: "strategy_id", label: "策略" },
						{ key: "version", label: "版本" },
						{ key: "period", label: "期間" },
						{ key: "total_return", label: "累積報酬", render: (r) => pnl({ value: r.total_return }) },
						{ key: "annualized_return", label: "年化" },
						{ key: "max_drawdown", label: "最大回撤" },
						{ key: "volatility", label: "波動率" },
						{ key: "trades", label: "交易次數" },
						{ key: "win_rate", label: "勝率" },
						{ key: "sel", label: "", render: (r) => h("button", { onClick: () => local.merge({ sel: r }) }, "檢視") }
					],
					empty: emptyState({ detail: "尚無回測結果鏡像。" })
				})),
			detail ? section({ title: `結果詳細：${String(detail.strategy_id || "")}` },
				h("div", { className: "inv-metric-grid" },
					["total_return", "annualized_return", "max_drawdown", "volatility", "trades", "win_rate"].map((k) =>
						metric({ label: k, value: num(detail[k]).toFixed(2) }))),
				investmentChart({ kind: "area", title: "資金曲線", points: curve, empty: "無資金曲線資料" }),
				dataTable({
					rows: arr(detail?.trades_detail || detail?.trades_list),
					columns: [
						{ key: "at", label: "時間" },
						{ key: "side", label: "方向" },
						{ key: "quantity", label: "數量" },
						{ key: "price", label: "價格" },
						{ key: "pnl", label: "損益", render: (r) => pnl({ value: r.pnl }) }
					],
					empty: emptyState({ kind: "insufficient", detail: "無交易明細。" })
				})) : null,
			section({ title: "驗證與比較" },
				h("div", { className: "inv-cards" },
					["版本比較", "樣本內驗證", "樣本外驗證", "Walk-Forward", "SHADOW 結果", "PAPER 結果"].map((k) =>
						h("article", { className: "inv-card" },
							h("b", null, k),
							emptyState({ kind: "insufficient", detail: "對應驗證資料送達後自動顯示。" })))),
				h("p", { className: "inv-note" }, "星澄可解讀回測結果（經 AI 研究通道）；模型不會生成不存在的績效數字。")));
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
