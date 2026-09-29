//! shell.js — ai-assistant workspace shell (React-free, E180/C116).
//! Replaces the retired ``InvestmentWorkspace`` component: left
//! navigation / top market+system status / center page / collapsible
//! 星澄 panel / bottom notification bar.  ``mountInvestmentWorkspace``
//! returns {el, destroy}; the active page is mounted per navigation and
//! destroyed on switch — subscriptions never leak across pages.
import { h, rerender } from "../../../../../shared-layer/src/ui/toolWindow/dom.js";
import { createLocalBackendSocket } from "../backendSocket.js";
import { createUIState } from "./uiState.js";
import { createCommandQuery, onIpcEvents } from "./hooks.js";
import { mountXingchengPanel } from "./XingchengPanel.js";

const NAV = [
	{ group: "總覽", items: [
		{ id: "overview", label: "全資產首頁" },
		{ id: "tw-equity", label: "台灣股票" },
		{ id: "us-equity", label: "美國股票" },
		{ id: "fund", label: "共同基金" }
	] },
	{ group: "AI 與建議", items: [
		{ id: "recommendations", label: "AI 買賣建議" },
		{ id: "reports", label: "投資報告" },
		{ id: "allocation", label: "資產配置" }
	] },
	{ group: "自動模擬操盤", items: [
		{ id: "autotrade", label: "自動操盤控制台" },
		{ id: "strategies", label: "多策略管理" },
		{ id: "laboratory", label: "策略實驗室" },
		{ id: "risk", label: "風險中心" }
	] },
	{ group: "系統", items: [
		{ id: "import", label: "資料匯入" },
		{ id: "brokers", label: "券商管理" },
		{ id: "settings", label: "系統設定" }
	] }
];
// Page modules are loaded on demand — only the active page mounts.
const PAGE_MODULES = {
	overview: () => import("./pages/OverviewPage.js"),
	"tw-equity": () => import("./pages/TaiwanEquityPage.js"),
	"us-equity": () => import("./pages/USEquityPage.js"),
	fund: () => import("./pages/MutualFundPage.js"),
	recommendations: () => import("./pages/RecommendationPage.js"),
	autotrade: () => import("./pages/AutoTradingDashboard.js"),
	strategies: () => import("./pages/StrategyManagementPage.js"),
	laboratory: () => import("./pages/StrategyLaboratoryPage.js"),
	allocation: () => import("./pages/AssetAllocationPage.js"),
	risk: () => import("./pages/RiskCenterPage.js"),
	reports: () => import("./pages/ReportPage.js"),
	import: () => import("./pages/ImportPage.js"),
	brokers: () => import("./pages/BrokerManagementPage.js"),
	settings: () => import("./pages/SettingsPage.js")
};

export function mountInvestmentWorkspace(root) {
	const ui = createUIState();
	const socket = createLocalBackendSocket({ closedMessage: "投資管家視窗已關閉，指令未送出。" });
	const ctx = {
		send: socket.sendCommand,
		ui,
		notify: ui.notify,
		query: (command, payload, isEmpty) => createCommandQuery(socket.sendCommand, command, payload, isEmpty),
		onEvent: (name, handler) => {
			const listener = (e) => {
				const d = e.detail;
				if (d?.event === name) handler(d.payload);
			};
			window.addEventListener("ipc_event", listener);
			return () => window.removeEventListener("ipc_event", listener);
		},
		onEvents: onIpcEvents
	};

	// --- top bar -------------------------------------------------------------
	const modeText = h("b", null, String(ui.get().mode));
	const liveHint = h("i", null, "LIVE 未啟用");
	const twMarket = h("span", { className: "ws-market" }, "台股 —");
	const usMarket = h("span", { className: "ws-market" }, "美股 —");
	const connText = h("span", { className: "ws-conn" }, `後端：${socket.getStatus()}`);
	const themeBtn = h("button", { onClick: () => ui.dispatch({ type: "theme", theme: ui.get().theme === "dark" ? "light" : "dark" }) });
	const panelBtn = h("button", { onClick: () => ui.dispatch({ type: "aiPanel", open: !ui.get().aiPanelOpen }) });
	const topbar = h("header", { className: "ws-topbar" },
		h("span", { className: "ws-mode" }, "模式 ", modeText, liveHint),
		twMarket, usMarket, connText,
		h("span", { className: "ws-controls" },
			h("button", { title: "字體縮小", onClick: () => ui.dispatch({ type: "fontScale", fontScale: ui.get().fontScale - .1 }) }, "A−"),
			h("button", { title: "字體放大", onClick: () => ui.dispatch({ type: "fontScale", fontScale: ui.get().fontScale + .1 }) }, "A+"),
			themeBtn, panelBtn));
	const market = ctx.query("investment_market_status");
	const mode = ctx.query("investment_trade_mode");
	const patchTopbar = () => {
		const m = market.get().data;
		const md = mode.get().data;
		const tw = m?.tw || m?.markets?.tw;
		const us = m?.us || m?.markets?.us;
		modeText.textContent = String(md?.mode || ui.get().mode);
		liveHint.style.display = String(md?.mode) !== "LIVE" ? "" : "none";
		twMarket.textContent = `台股 ${String(tw?.session || tw?.status || "—")}`;
		usMarket.textContent = `美股 ${String(us?.session || us?.status || "—")}`;
		connText.textContent = `後端：${socket.getStatus()}`;
		const s = ui.get();
		themeBtn.textContent = s.theme === "dark" ? "淺色" : "深色";
		panelBtn.textContent = s.aiPanelOpen ? "收起星澄" : "星澄助手";
	};
	const unsubMarket = market.subscribe(patchTopbar);
	const unsubMode = mode.subscribe(patchTopbar);
	const unsubConn = socket.subscribeStatus(patchTopbar);

	// --- side nav --------------------------------------------------------------
	const navHost = h("div");
	const renderNav = () => {
		const s = ui.get();
		rerender(navHost, () => {
			if (s.sidebarCollapsed) {
				return h("nav", { className: "ws-nav ws-nav-collapsed" },
					h("button", {
						className: "ws-nav-expand",
						onClick: () => ui.dispatch({ type: "sidebar", collapsed: false })
					}, "»"),
					NAV.flatMap((g) => g.items).map((i) => h("button", {
						title: i.label,
						className: i.id === s.page ? "active" : "",
						onClick: () => ui.dispatch({ type: "page", page: i.id })
					}, i.label.slice(0, 2))));
			}
			return h("nav", { className: "ws-nav" },
				h("button", {
					className: "ws-nav-expand",
					onClick: () => ui.dispatch({ type: "sidebar", collapsed: true })
				}, "« 收合"),
				NAV.map((g) => h("div", { className: "ws-nav-group" },
					h("span", { className: "ws-nav-group-label" }, g.group),
					g.items.map((i) => h("button", {
						className: i.id === s.page ? "active" : "",
						onClick: () => ui.dispatch({ type: "page", page: i.id })
					}, i.label)))));
		});
	};

	// --- bottom bar --------------------------------------------------------------
	const noticeText = h("span", { className: "ws-notice" }, "無通知");
	const bottombar = h("footer", { className: "ws-bottombar" },
		noticeText,
		h("span", { className: "ws-bottom-hint" }, "所有交易為 SHADOW/PAPER 模擬；券商連線離線"));
	const patchNotices = () => {
		const latest = ui.get().notices[0];
		if (latest) {
			noticeText.textContent = `${new Date(latest.at).toLocaleTimeString("zh-TW", { hour12: false })} ${latest.text}`;
			noticeText.className = `ws-notice ws-notice-${latest.severity}`;
		} else {
			noticeText.textContent = "無通知";
			noticeText.className = "ws-notice";
		}
	};

	// --- page + 星澄 panel hosts ---------------------------------------------------
	const pageHost = h("main", { className: "ws-main" });
	const xingHost = h("div");
	let activePage = null; // {destroy()}
	let pageGeneration = 0;
	const mountPage = async (pageId) => {
		const generation = ++pageGeneration;
		if (activePage) {
			try { activePage.destroy?.(); } catch {}
			activePage = null;
		}
		pageHost.replaceChildren(h("div", { className: "inv-empty" }, "載入中…"));
		const load = PAGE_MODULES[pageId] || PAGE_MODULES.overview;
		try {
			const mod = await load();
			if (generation !== pageGeneration) return;
			const mounted = mod.mount(ctx);
			activePage = mounted;
			pageHost.replaceChildren(mounted.el);
		} catch (error) {
			if (generation !== pageGeneration) return;
			pageHost.replaceChildren(h("div", { className: "inv-empty" },
				`頁面載入失敗：${error instanceof Error ? error.message : String(error)}`));
		}
	};

	let xingPanel = null;
	const syncPanel = () => {
		const open = ui.get().aiPanelOpen;
		if (open && !xingPanel) {
			xingPanel = mountXingchengPanel(ctx);
			xingHost.replaceChildren(xingPanel.el);
		} else if (!open && xingPanel) {
			xingPanel.destroy();
			xingPanel = null;
			xingHost.replaceChildren();
		}
	};

	// Event-driven updates — one subscription at the shell, pages read
	// fresh state via their own bounded queries (§20)
	const unsubIpc = onIpcEvents((event, payload) => {
		const p = payload;
		if (!p) return;
		if (event === "investment-mobile-monitor-alert" || event.endsWith("_alert")) {
			ui.notify(`風控/警示：${String(p.title || p.event_type || event)}`, String(p.severity || "WARNING"));
		}
		if (event === "record_mode" && p.mode) {
			ui.dispatch({ type: "mode", mode: String(p.mode) });
		}
	});

	const el = h("div", { className: `ws-app theme-${ui.get().theme}` });
	const body = h("div", { className: "ws-body" }, navHost, pageHost, xingHost);
	el.append(topbar, body, bottombar);
	root.replaceChildren(el);

	let lastPage = "";
	const unsubUi = ui.subscribe((s) => {
		el.className = `ws-app theme-${s.theme}`;
		el.style.fontSize = `${s.fontScale}rem`;
		renderNav();
		syncPanel();
		patchTopbar();
		patchNotices();
		if (s.page !== lastPage) {
			lastPage = s.page;
			void mountPage(s.page);
		}
	});
	// initial paint
	{
		const s = ui.get();
		renderNav();
		syncPanel();
		patchTopbar();
		patchNotices();
		lastPage = s.page;
		void mountPage(s.page);
	}

	return {
		el,
		destroy() {
			pageGeneration += 1;
			unsubUi();
			unsubIpc();
			unsubMarket();
			unsubMode();
			unsubConn();
			market.destroy();
			mode.destroy();
			if (activePage) { try { activePage.destroy?.(); } catch {} activePage = null; }
			if (xingPanel) { xingPanel.destroy(); xingPanel = null; }
			socket.destroy();
		}
	};
}
