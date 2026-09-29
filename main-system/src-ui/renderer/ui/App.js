import { createAppState } from "./appState.js";
import { createToolboxApplications } from "./toolbox/toolboxApplications.js";
import { mountToolboxEntry } from "./toolbox/ToolboxEntry.js";
import { mountSovereignDashboard } from "./sovereign/SovereignDashboard.js";
import { createDrawer } from "./drawer/Drawer.js";
import { createXingchengDrawer } from "./drawer/XingchengDrawer.js";
import { createCapacityDrawer } from "./drawer/CapacityDrawer.js";
import { createSloDrawer } from "./drawer/SloDrawer.js";
import { createThirdPartyPanel } from "./panel/ThirdPartyPanel.js";
import { createSagaVisualizerPanel } from "./panel/SagaVisualizerPanel.js";
import { getRuntimeStatusField, subscribeRuntimeStatusField } from "@/shared/services/runtimeStatusField.js";
import { filterActiveFaults } from "@/shared/utils/faultPresentation";
import { mainSystemLocale } from "@/locales/main-system";
import { guardedUpdate, h, mountBoundary } from "@/shared/mini/dom.js";
import "../App.css";
const t = mainSystemLocale.product;
const xr = mainSystemLocale.xingchengReport;
const tb = mainSystemLocale.toolbox;
function displayVersion(value) {
	const match = /^(\d+)\.(\d+)(?:\.\d+)?$/.exec(value.trim());
	return match ? `${match[1]}.${match[2]}` : "1.0";
}
function computeReview({ backendStatus, connected, maintenanceReady, globalFaults, activeGlobalFaults, toolboxTools }) {
	if (backendStatus === "Connecting" || backendStatus === "Synchronizing") {
		return {
			tone: "warning",
			state: xr.reviewing,
			detail: xr.reviewingDetail,
			issues: [{
				id: "backend-connecting",
				source: xr.informationLayer,
				title: xr.backendInterrupted,
				detail: xr.reviewingDetail,
				status: xr.autoRepairing
			}]
		};
	}
	if (!connected) {
		return {
			tone: "warning",
			state: xr.connectionAnomaly,
			detail: xr.connectionDetail,
			issues: [{
				id: "backend-offline",
				source: xr.informationLayer,
				title: xr.backendDisconnected,
				detail: xr.backendDisconnectedDetail,
				status: xr.waitingRecovery
			}]
		};
	}
	if (!maintenanceReady) {
		return {
			tone: "warning",
			state: xr.healthAnomaly,
			detail: xr.healthDetail,
			issues: [{
				id: "maintenance-not-ready",
				source: xr.maintenanceSovereign,
				title: xr.maintenanceNotReady,
				detail: xr.maintenanceNotReadyDetail,
				status: xr.monitoring
			}]
		};
	}
	const unresolvedFaults = Number(globalFaults?.unresolved || 0);
	if (globalFaults && unresolvedFaults > 0) {
		const issues = activeGlobalFaults.slice(0, 5).map((fault, index) => ({
			id: `global-fault-${String(fault.fault_id || index)}`,
			source: String(fault.source || xr.globalFaultsTitle),
			title: String(fault.error_class || fault.fault_type || xr.toolError),
			detail: String(fault.error_message || xr.noFurtherDetail),
			status: String(fault.repair_outcome || "pending")
		}));
		const severityCounts = {};
		for (const fault of activeGlobalFaults) {
			const severity = String(fault.severity || "info");
			severityCounts[severity] = (severityCounts[severity] || 0) + 1;
		}
		const severity = Object.entries(severityCounts).map(([key, count]) => `${key} ${count}`).join("、");
		return {
			tone: "warning",
			state: xr.globalFaultsState.replace("{count}", String(unresolvedFaults)),
			detail: severity || xr.globalFaultsHint,
			issues
		};
	}
	const affected = toolboxTools.filter((tool) => tool.status === "error" || tool.launchable !== false && tool.runtimeAvailable === false);
	const issues = affected.map((tool) => ({
		id: `tool-${tool.id}`,
		source: tool.name,
		title: tool.status === "error" ? xr.toolError : xr.runtimeUnavailable,
		detail: tool.note || tool.description || tool.summary || xr.noFurtherDetail,
		status: tool.status === "error" ? xr.needsAction : xr.waitingRuntime
	}));
	if (affected.length > 0) {
		return {
			tone: "warning",
			state: `${affected.length} ${xr.affectedSuffix}`,
			detail: affected.slice(0, 3).map((tool) => tool.name).join("、"),
			issues
		};
	}
	return {
		tone: "ok",
		state: xr.normal,
		detail: xr.normalDetail,
		issues: []
	};
}

/**
* mountApp — former App.jsx.  Builds the product shell once and patches
* the dynamic regions on store/socket/field updates (same modular-refresh
* contract the hook version had).  Returns {el, destroy()}.
*/
export function mountApp() {
	const appState = createAppState();
	const sendCommand = appState.get().sendCommand;
	const waitForIpcEvent = appState.get().waitForIpcEvent;
	const toolbox = createToolboxApplications({
		socketState: () => appState.get().backendSocket,
		subscribeSocket: (listener) => appState.subscribeSocket(listener),
		sendCommand,
		waitForIpcEvent
	});
	const drawers = {
		sovereign: false,
		xingcheng: false,
		capacity: false,
		slo: false,
		thirdParty: false,
		sagaVisualizer: false
	};
	let currentReview = computeReview({
		backendStatus: appState.get().backendSocket.status,
		connected: appState.get().connected,
		maintenanceReady: appState.get().maintenanceReady,
		globalFaults: null,
		activeGlobalFaults: [],
		toolboxTools: []
	});

	// --- dynamic regions -----------------------------------------------------
	const nativeModelIndicator = h("div", {
		className: "connection-indicator",
		dataset: { tone: "pending", testid: "xingcheng-native-model-indicator" },
		title: xr.nativeModelTitle
	},
		h("span", { className: "connection-indicator__dot" }),
		h("span", null,
			h("strong", null, xr.nativeModelTitle),
			h("small", null, xr.nativeModelUnavailable)));
	const connectionIndicator = h("div", {
		className: "connection-indicator",
		dataset: { tone: "offline", testid: "backend-connection" }
	},
		h("span", { className: "connection-indicator__dot" }),
		h("span", null,
			h("strong", null, xr.systemAnomaly),
			h("small", null, xr.connectionDetail)));
	const versionBadge = h("span", { className: "version-badge", dataset: { testid: "product-version" } }, "v1.0");
	const notice = h("aside", {
		className: "connection-notice",
		role: "status",
		style: { display: "none" }
	}, h("strong"), h("span"));
	const heroTotal = h("strong", { className: "hero-card__value" }, "0");
	const heroRunning = h("strong", { className: "hero-card__value" }, "0");
	const heroIssuesCard = h("article", { className: "hero-card", dataset: { tone: "ok" } },
		h("span", { className: "hero-card__label" }, t.issues),
		h("strong", { className: "hero-card__value" }, "0"),
		h("small", { className: "hero-card__hint" }, t.issuesHint));
	const reviewCard = h("button", {
		type: "button",
		className: "hero-card hero-card--interactive",
		dataset: { tone: "ok", testid: "xingcheng-global-review" },
		"aria-haspopup": "dialog",
		onClick: () => {
			setDrawer("xingcheng", true);
			// Periodic pushes are compact; fetch the full snapshot (with
			// the per-item pending list) when the panel opens.
			sendCommand("app:get-runtime-status", { source: "xingcheng_drawer_open" });
		}
	},
		h("span", { className: "hero-card__label" }, mainSystemLocale.sovereign.xingchengTitle),
		h("strong", { className: "hero-card__value hero-card__value--text" }, xr.normal),
		h("small", { className: "hero-card__hint" }, xr.normalDetail),
		h("span", { className: "hero-card__action" }, xr.viewDetails));

	const toolboxEntry = mountBoundary(tb.title, () => mountToolboxEntry(() => {
		const tb2 = toolbox.get();
		return {
			tools: tb2.toolboxTools,
			connected: appState.get().operational,
			syncing: tb2.toolboxSyncing,
			syncedAt: tb2.toolboxSyncedAt,
			onRefresh: () => void toolbox.refreshToolboxTools(),
			onToolAction: toolbox.handleToolboxAction
		};
	}));
	const toolboxSlot = h("div");
	if (toolboxEntry?.el) toolboxSlot.appendChild(toolboxEntry.el);

	const setDrawer = (name, open) => {
		drawers[name] = open;
		drawerApi[name]?.setOpen(open);
	};

	let sovereignMount = null;
	const drawerApi = {
		xingcheng: mountBoundary(xr.title, () => createXingchengDrawer({
			onClose: () => setDrawer("xingcheng", false),
			getProps: () => {
				const s = appState.get();
				return {
					review: currentReview,
					confirmBusyId: s.confirmBusyId,
					confirmMessages: s.confirmMessages,
					switchBusy: s.switchBusy,
					onConfirm: s.confirmPendingAction,
					onDeny: s.denyPendingAction,
					onSwitch: s.setAutomationSwitch,
					onNativeModelSwitch: s.setXingchengNativeModelEnabled,
					sendCommand,
					waitForIpcEvent,
					onOpenSovereign: () => setDrawer("sovereign", true),
					onOpenCapacity: () => setDrawer("capacity", true),
					onOpenSlo: () => setDrawer("slo", true),
					onOpenThirdParty: () => setDrawer("thirdParty", true),
					onOpenSaga: () => setDrawer("sagaVisualizer", true)
				};
			}
		})),
		sovereign: createDrawer({
			onClose: () => setDrawer("sovereign", false),
			title: mainSystemLocale.sovereign.title,
			eyebrow: mainSystemLocale.sovereign.eyebrow,
			icon: "S",
			render: (body) => {
				if (!sovereignMount) {
					sovereignMount = mountBoundary("主權面板", () => mountSovereignDashboard({}));
				}
				if (sovereignMount?.el) body.appendChild(sovereignMount.el);
			}
		}),
		thirdParty: mountBoundary("第三方軟體", () => createThirdPartyPanel({
			onClose: () => setDrawer("thirdParty", false),
			sendCommand,
			waitForIpcEvent,
			getConnected: () => appState.get().connected
		})),
		sagaVisualizer: mountBoundary(mainSystemLocale.sagaVisualizer.moduleBoundaryName, () => createSagaVisualizerPanel({
			onClose: () => setDrawer("sagaVisualizer", false),
			sendCommand,
			waitForIpcEvent,
			getConnected: () => appState.get().connected
		})),
		capacity: mountBoundary("容量資訊", () => createCapacityDrawer({
			onClose: () => setDrawer("capacity", false),
			getProps: () => {
				const s = appState.get();
				const tb2 = toolbox.get();
				return {
					systemMetrics: s.systemMetrics,
					mainSystemSizeBytes: tb2.mainSystemSizeBytes,
					mainSystemFileCount: tb2.mainSystemFileCount,
					dependencySizeBytes: tb2.dependencySizeBytes,
					dependencyFileCount: tb2.dependencyFileCount,
					sharedLayerSizeBytes: tb2.sharedLayerSizeBytes,
					sharedLayerFileCount: tb2.sharedLayerFileCount,
					workspaceSizeBytes: tb2.workspaceSizeBytes,
					workspaceFileCount: tb2.workspaceFileCount
				};
			}
		})),
		slo: mountBoundary("效能 SLO", () => createSloDrawer({
			onClose: () => setDrawer("slo", false)
		}))
	};

	function update() {
		const s = appState.get();
		const tbState = toolbox.get();
		const globalFaults = getRuntimeStatusField("global_faults");
		const nativeModel = getRuntimeStatusField("xingcheng_native_model_runtime");
		const activeGlobalFaults = filterActiveFaults(globalFaults?.recent_faults);

		nativeModelIndicator.dataset.tone = nativeModel?.running ? "online" : nativeModel?.available ? "offline" : "pending";
		nativeModelIndicator.querySelector("small").textContent = nativeModel?.running ? xr.nativeModelRunning : nativeModel?.available ? xr.nativeModelStopped : xr.nativeModelUnavailable;

		const connection = s.connected ? {
			label: xr.systemNormal,
			detail: xr.normalDetail,
			tone: "online"
		} : s.backendSocket.status === "Connecting" || s.backendSocket.status === "Synchronizing" ? {
			label: xr.systemReviewing,
			detail: xr.reviewingDetail,
			tone: "pending"
		} : {
			label: xr.systemAnomaly,
			detail: xr.connectionDetail,
			tone: "offline"
		};
		connectionIndicator.dataset.tone = connection.tone;
		const connSpans = connectionIndicator.querySelectorAll("strong, small");
		connSpans[0].textContent = connection.label;
		connSpans[1].textContent = connection.detail;

		versionBadge.textContent = `v${displayVersion(s.appVersion)}`;
		footerVersion.textContent = `GPTBridge v${displayVersion(s.appVersion)}`;

		if (!s.operational) {
			notice.style.display = "";
			notice.firstChild.textContent = s.connected ? t.maintenanceIncomplete : t.offlineSafeMode;
			notice.lastChild.textContent = s.connected ? t.maintenanceNotice : t.offlineNotice;
		} else {
			notice.style.display = "none";
		}

		const running = tbState.toolboxTools.filter((tool) => tool.status === "running").length;
		const issues = tbState.toolboxTools.filter((tool) => tool.status === "error" || tool.launchable !== false && tool.runtimeAvailable === false).length;
		heroTotal.textContent = String(tbState.toolboxTools.length);
		heroRunning.textContent = String(running);
		heroIssuesCard.dataset.tone = issues > 0 ? "warning" : "ok";
		heroIssuesCard.querySelector(".hero-card__value").textContent = String(issues);

		currentReview = computeReview({
			backendStatus: s.backendSocket.status,
			connected: s.connected,
			maintenanceReady: s.maintenanceReady,
			globalFaults,
			activeGlobalFaults,
			toolboxTools: tbState.toolboxTools
		});
		reviewCard.dataset.tone = currentReview.tone;
		reviewCard.querySelector(".hero-card__value").textContent = currentReview.state;
		reviewCard.querySelector(".hero-card__hint").textContent = currentReview.detail;

		// Panel chrome sees reconnects (former backendSocket.status prop).
		drawerApi.thirdParty?.setConnected?.(s.connected);
		drawerApi.sagaVisualizer?.setConnected?.(s.connected);

		if (toolboxEntry?.update) guardedUpdate(tb.title, () => toolboxEntry.update());
	}

	const footerVersion = h("span", null, "GPTBridge v1.0");
	const el = h("div", { className: "product-shell" },
		h("header", { className: "product-header" },
			h("div", { className: "brand-lockup" },
				h("div", { className: "brand-mark", "aria-hidden": "true" }, "G"),
				h("div", null,
					h("strong", null, "GPTBridge"),
					h("span", null, t.brandSubtitle))),
			h("div", { className: "header-controls" },
				nativeModelIndicator,
				connectionIndicator,
				versionBadge)),
		h("main", { className: "product-main" },
			notice,
			h("section", { className: "hero-grid", "aria-label": t.systemOverview },
				h("article", { className: "hero-card hero-card--primary" },
					h("span", { className: "hero-card__label" }, t.availableTools),
					heroTotal,
					h("small", { className: "hero-card__hint" }, t.availableToolsHint)),
				h("article", { className: "hero-card" },
					h("span", { className: "hero-card__label" }, t.runningTools),
					heroRunning,
					h("small", { className: "hero-card__hint" }, t.runningToolsHint)),
				heroIssuesCard,
				h("article", { className: "hero-card" },
					h("span", { className: "hero-card__label" }, t.commandStrategy),
					h("strong", { className: "hero-card__value hero-card__value--text" }, t.requestToolExecution),
					h("small", { className: "hero-card__hint" }, t.commandStrategyHint)),
				reviewCard),
			toolboxSlot),
		h("footer", { className: "product-footer" },
			footerVersion,
			h("span", null, t.footerPlatform)));

	const unsubs = [
		appState.subscribe(() => update()),
		appState.subscribeSocket(() => update()),
		toolbox.subscribe(() => update()),
		subscribeRuntimeStatusField("global_faults", () => update()),
		subscribeRuntimeStatusField("xingcheng_native_model_runtime", () => update())
	];
	update();
	return {
		el,
		destroy() {
			for (const unsub of unsubs) unsub();
			for (const api of Object.values(drawerApi)) api?.destroy?.();
			toolboxEntry?.destroy?.();
			sovereignMount?.destroy?.();
			toolbox.destroy();
			appState.destroy();
		}
	};
}
export { mountApp as default };
