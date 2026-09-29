//! XingchengPanel.js — 星澄 analysis side panel (React-free,
//! E180/C116).  Natural-language intents map to governed commands; every
//! answer arrives through the backend with deterministic data attached,
//! never raw model text alone.  ``mountXingchengPanel(ctx)`` returns
//! {el, destroy}.
import { createStore, h, rerender } from "../../../../../shared-layer/src/ui/toolWindow/dom.js";
import { emptyState } from "./common.js";

const INTENTS = [
	{ key: "tw", label: "分析我的台股", command: "investment_tw_analysis" },
	{ key: "us", label: "分析我的美股", command: "investment_us_analysis" },
	{ key: "fund", label: "分析共同基金", command: "investment_fund_analysis" },
	{ key: "all", label: "分析總資產", command: "investment_ai_portfolio_analysis" },
	{ key: "overlap", label: "找出持倉重疊", command: "investment_assets_exposure" },
	{ key: "risk", label: "分析市場風險", command: "investment_ai_risk" },
	{ key: "report", label: "產生投資報告", command: "investment_ai_report" },
	{ key: "sim", label: "檢視模擬交易表現", command: "investment_sim_performance" }
];

export function mountXingchengPanel(ctx) {
	const store = createStore({ question: "", history: [], last: null });
	const ask = (label, command, extra) => {
		const ack = ctx.send(command, extra ?? {});
		store.merge({
			history: [{ q: label, event: `${command}_result`, at: Date.now() }, ...store.get().history].slice(0, 20)
		});
		if (!ack.ok) store.merge({ last: { ok: false, message: ack.message } });
	};
	// capture whichever command result arrives last
	const unsubIpc = ctx.onEvents((event, payload) => {
		if (event.endsWith("_result")) store.merge({ last: payload });
	});
	const freeText = () => {
		const q = store.get().question.trim();
		if (!q) return;
		// free-form research questions go through the governed search/consult
		// path — the model never reads its private store directly
		ask(q, "investment_ai_research_search", { query: q });
		store.merge({ question: "" });
	};

	const modelState = h("span", { className: "xing-model-state" });
	const input = h("input", {
		placeholder: "向星澄提問（市場研究）…",
		dataset: { k: "xing-question" },
		onInput: (e) => store.merge({ question: e.target.value }),
		onKeydown: (e) => { if (e.key === "Enter") freeText(); }
	});
	const answerHost = h("div", { className: "xing-answer" });
	const historyHost = h("ul", { className: "xing-history" });
	const el = h("aside", { className: "xing-panel" },
		h("header", { className: "xing-panel-head" },
			h("b", null, "星澄 AI 投資助手"), modelState),
		h("div", { className: "xing-intents" },
			INTENTS.map((i) => h("button", { onClick: () => ask(i.label, i.command) }, i.label))),
		h("div", { className: "xing-input" },
			input,
			h("button", { onClick: freeText }, "送出")),
		answerHost,
		historyHost);

	const render = () => {
		const s = store.get();
		const uiMode = ctx.ui.get().mode;
		modelState.textContent = `模型狀態：${uiMode === "OFFLINE" ? "離線" : "連線"}`;
		if (input.value !== s.question) input.value = s.question;
		const last = s.last;
		let text = "";
		if (last != null) {
			const p = last;
			text = p.ok === false
				? `無法完成：${String(p.error_code || p.message || "BACKEND_ERROR")}`
				: JSON.stringify(p, null, 2);
		}
		rerender(answerHost, () => text
			? h("pre", null, text)
			: emptyState({ detail: "選擇分析指令或輸入問題；所有數值來自正式資料與確定性計算。" }));
		rerender(historyHost, () => s.history.map((item) =>
			h("li", null, `${new Date(item.at).toLocaleTimeString("zh-TW", { hour12: false })} — ${item.q}`)));
	};
	const unsubStore = store.subscribe(render);
	const unsubUi = ctx.ui.subscribe(render);
	render();

	return {
		el,
		destroy() {
			unsubIpc();
			unsubStore();
			unsubUi();
		}
	};
}
