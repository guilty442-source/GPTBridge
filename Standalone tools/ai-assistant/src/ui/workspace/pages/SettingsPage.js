//! SettingsPage.js — 系統設定 (React-free, E180/C116).
//! Only whitelisted display-preference keys are writable through this
//! page — trading authorizations, risk limits, LIVE and broker flags are
//! never settable here (backend enforces; see asset_mgmt domain).
import { createStore, h, rerender } from "../../../../../../shared-layer/src/ui/toolWindow/dom.js";
import { badge, emptyState, section } from "../common.js";

const WRITABLE = [
	{ key: "display_currency", label: "顯示幣別", choices: ["TWD", "USD"] },
	{ key: "theme", label: "主題", choices: ["light", "dark"] },
	{ key: "font_scale", label: "字體縮放", type: "number" },
	{ key: "sidebar_collapsed", label: "側邊欄收合", choices: ["false", "true"] },
	{ key: "default_page", label: "預設頁面" },
	{ key: "watchlist_tw", label: "台股自選清單（逗號分隔）" },
	{ key: "watchlist_us", label: "美股自選清單（逗號分隔）" },
	{ key: "notification_mute", label: "通知靜音", choices: ["false", "true"] }
];
/** Categories the engine does not yet expose — shown honestly as
 * 整合未完成 instead of fake controls. */
const NOT_INTEGRATED = [
	"帳戶管理（引擎端管理）",
	"行情更新間隔",
	"基金更新排程",
	"星澄模型通道設定",
	"分析排程",
	"策略參數",
	"資料匯入匯出設定"
];
const DENIED = [
	"正式交易授權",
	"風控上限",
	"LIVE 模式",
	"broker_network_enabled",
	"券商憑證",
	"RUNNING 策略正式版本"
];
const HEALTH_NAMES = {
	lifecycle: "投資管家狀態",
	inference: "星澄模型狀態",
	subscriptions: "行情資料狀態",
	jobs: "背景工作狀態",
	autotrade: "模擬操盤狀態",
	budget: "資源預算",
	metrics: "運行監控",
	power: "電源狀態"
};

export function mount(ctx) {
	const local = createStore({ draft: {}, draftLoaded: false });
	const get = ctx.query("investment_settings_get");
	const health = ctx.query("investment_perf_health");
	const queries = [get, health];
	const unsubResult = ctx.onEvent("investment_settings_set_result", (p) => {
		const d = p;
		ctx.notify(
			d?.ok === false
				? `設定未儲存：${d.error_code || d.message}${Array.isArray(d?.denied) ? `（${d.denied.join(", ")}）` : ""}`
				: "設定已儲存",
			d?.ok === false ? "WARNING" : "INFO");
	});
	const save = () => {
		const ack = ctx.send("investment_settings_set", { settings: local.get().draft });
		if (!ack.ok) ctx.notify(`設定未送出：${ack.message}`, "ERROR");
	};
	const el = h("div");
	const render = () => rerender(el, () => {
		const s = local.get();
		const gd = get.get().data;
		const writable = new Set(Array.isArray(gd?.writable_keys) ? gd.writable_keys.map(String) : WRITABLE.map((w) => w.key));
		const hv = health.get().data?.view;
		const subs = hv?.subsystems || {};
		return h("div", { className: "inv-page" },
			h("header", { className: "inv-page-head" },
				h("h2", null, "系統設定"),
				h("button", { className: "inv-primary", onClick: save }, "儲存")),
			section({ title: "一般與顯示設定" },
				h("div", { className: "inv-form" },
					WRITABLE.filter((w) => writable.has(w.key)).map((w) => h("label", null, w.label,
						w.choices
							? h("select", {
								dataset: { k: `set-${w.key}` },
								onChange: (e) => local.merge({ draft: { ...local.get().draft, [w.key]: e.target.value } })
							},
								h("option", { value: "", selected: !String(s.draft[w.key] ?? "") }, "—"),
								w.choices.map((c) => h("option", { value: c, selected: String(s.draft[w.key] ?? "") === c }, c)))
							: h("input", {
								type: w.type || "text",
								value: String(s.draft[w.key] ?? ""),
								dataset: { k: `set-${w.key}` },
								onInput: (e) => local.merge({
									draft: { ...local.get().draft, [w.key]: w.type === "number" ? Number(e.target.value) : e.target.value }
								})
							})))),
				gd?.note ? h("p", { className: "inv-note" }, String(gd.note)) : null),
			section({ title: "尚未整合的設定類別" },
				h("ul", { className: "inv-denied" },
					NOT_INTEGRATED.map((k) => h("li", null, badge({ text: `${k}——整合未完成`, tone: "muted" }))))),
			section({ title: "系統健康" },
				!health.get().data || !hv || !Object.keys(subs).length
					? emptyState({ kind: "insufficient", detail: "引擎健康鏡像尚未送達——investment-mobile 推送 perf-health 後顯示。" })
					: [
						h("p", { className: "inv-note" },
							`整體：${String(hv.overall || "—")}`,
							Array.isArray(hv.degraded) && hv.degraded.length > 0 ? `　降級：${hv.degraded.join(", ")}` : ""),
						h("div", { className: "inv-cards" },
							Object.entries(HEALTH_NAMES).map(([k, label]) => {
								const st = subs[k] || {};
								return h("article", { className: "inv-card" },
									h("b", null, label),
									badge({ text: st.ok === false ? "異常" : "正常", tone: st.ok === false ? "danger" : "ok" }),
									h("p", { className: "inv-note" },
										st.state ? `狀態 ${st.state}　` : "",
										st.error_code ? `錯誤 ${st.error_code}` : ""));
							}))
					],
				h("p", { className: "inv-note" }, "模型可用不代表全部投資服務正常——各子系統獨立回報。")),
			section({ title: "受治理保護（不可經此頁修改）" },
				h("ul", { className: "inv-denied" },
					DENIED.map((k) => h("li", null, badge({ text: k, tone: "danger" })))),
				h("p", { className: "inv-note" }, "後端拒絕非白名單鍵（SETTING_KEY_DENIED）；前端欄位非安全防護。")));
	});
	// hydrate the draft once settings arrive (React effect-on-data parity)
	const unsubGet = get.subscribe(() => {
		const settings = get.get().data?.settings;
		if (settings && !local.get().draftLoaded) {
			local.merge({ draft: settings, draftLoaded: true });
		}
	});
	const unsubs = [local.subscribe(render), ...queries.map((q) => q.subscribe(render)), unsubGet];
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
