//! BrokerManagementPage.js — 券商管理 (React-free, E180/C116).
import { h, rerender } from "../../../../../../shared-layer/src/ui/toolWindow/dom.js";
import { dataFreshnessIndicator } from "../freshness.js";
import { badge, dataTable, emptyState, metric, section } from "../common.js";

const arr = (v) => Array.isArray(v) ? v : [];
const BROKERS = [
	{ key: "cathay", name: "國泰綜合證券", market: "TW", type: "台股" },
	{ key: "fubon", name: "富邦綜合證券複委託", market: "US", type: "美股複委託" }
];
function brokerState(b) {
	if (b.connected === true) return "CONNECTED";
	if (b.mock === true || b.simulated === true) return "MOCK";
	if (b.ready === true) return "READY_FOR_INTEGRATION";
	return "OFFLINE";
}

export function mount(ctx) {
	const status = ctx.query("investment_broker_status");
	const accounts = ctx.query("investment_broker_accounts");
	const sims = ctx.query("investment_broker_sims");
	const queries = [status, accounts, sims];
	const el = h("div");
	const render = () => rerender(el, () => {
		const s = status.get().data || {};
		const gate = s.offline_gate || s.gate || {};
		return h("div", { className: "inv-page" },
			h("header", { className: "inv-page-head" },
				h("h2", null, "券商管理"),
				h("span", { className: "freshness freshness-unavailable" },
					h("b", null, "本階段"), h("i", null, "真實券商連線停用"))),
			h("div", { className: "inv-metric-grid" },
				metric({ label: "券商網路", value: gate.broker_network_enabled ? "啟用" : "OFFLINE", tone: "down" }),
				metric({ label: "真實交易", value: gate.live_trading_enabled ? "啟用" : "停用", tone: "down" }),
				metric({ label: "券商驗證", value: gate.broker_authentication_enabled ? "啟用" : "停用", tone: "down" }),
				metric({ label: "閘門狀態", value: "OFFLINE_GATE_LOCKED", sub: "前端無法切換——無 setter 暴露" })),
			h("div", { className: "inv-cards" },
				BROKERS.map((b) => {
					const rec = arr(accounts.get().data?.accounts).find((a) =>
						String(a.broker_id || a.broker || "").toLowerCase().includes(b.key)) || {};
					return h("article", { className: "inv-card" },
						h("header", { className: "inv-card-head" },
							h("b", null, b.name),
							badge({ text: brokerState(rec), tone: brokerState(rec) === "OFFLINE" ? "warn" : "info" })),
						h("dl", { className: "inv-card-meta" },
							h("dt", null, "市場"), h("dd", null, b.market),
							h("dt", null, "帳戶類型"), h("dd", null, b.type),
							h("dt", null, "資料來源"), h("dd", null, String(rec.source || "MANUAL / FILE_IMPORT")),
							h("dt", null, "整合狀態"), h("dd", null, String(rec.integration_status || brokerState(rec)))),
						dataFreshnessIndicator({ record: rec }));
				})),
			section({ title: "帳戶清單" },
				dataTable({
					rows: arr(accounts.get().data?.accounts),
					columns: [
						{ key: "account_id", label: "帳戶" },
						{ key: "broker_id", label: "券商" },
						{ key: "market", label: "市場" },
						{ key: "account_type", label: "類型" },
						{ key: "source", label: "資料來源" },
						{ key: "integration_status", label: "整合狀態" },
						{ key: "updated_at", label: "最後更新", render: (r) => dataFreshnessIndicator({ record: r }) }
					],
					empty: emptyState({ detail: "尚無帳戶資料。" })
				})),
			section({ title: "模擬券商（PAPER）" },
				dataTable({
					rows: arr(sims.get().data?.simulations || sims.get().data?.accounts),
					columns: [
						{ key: "account_id", label: "模擬帳戶" },
						{ key: "market", label: "市場" },
						{ key: "cash", label: "模擬資金" },
						{ key: "positions", label: "持倉數" },
						{ key: "status", label: "狀態" }
					],
					empty: emptyState({ detail: "尚無模擬券商帳戶。" })
				})),
			h("p", { className: "inv-note" }, "本階段不提供：真實登入、API 憑證輸入、真實下單、真實帳戶同步。broker_network_enabled 不可由前端切換。"));
	});
	const unsubs = queries.map((q) => q.subscribe(render));
	render();
	return {
		el,
		destroy() {
			unsubs.forEach((u) => u());
			queries.forEach((q) => q.destroy());
		}
	};
}
