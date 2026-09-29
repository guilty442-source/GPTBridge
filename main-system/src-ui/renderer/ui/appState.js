import { serviceManager, startStartupPipeline } from "@/services/RuntimeServiceManager";
import { createBackendSocket } from "@/shared/services/backendSocket";
import { mainSystemLocale } from "@/locales/main-system";
import { applyRuntimeStatusReport, getRuntimeStatusState } from "@/shared/services/runtimeStatusStore";
import { createStore } from "@/shared/mini/dom.js";
const UI_ZOOM_STORAGE_KEY = "gptbridge_ui_zoom_factor";
const MIN_UI_ZOOM = .85;
const MAX_UI_ZOOM = 1.3;
const t = mainSystemLocale.product;
const xr = mainSystemLocale.xingchengReport;
function clampUiZoom(value) {
	return Math.max(MIN_UI_ZOOM, Math.min(MAX_UI_ZOOM, value));
}
// Perf/low-render: backend pushes status every ~2s with a fresh metrics
// object each time. Skip setState when all fields are identical so the
// whole App does not re-render on an unchanged sample.
function sameSystemMetrics(a, b) {
	return a.diskUsagePercent === b.diskUsagePercent && a.diskTotalBytes === b.diskTotalBytes && a.diskFreeBytes === b.diskFreeBytes && a.diskRoot === b.diskRoot;
}

/**
* createAppState — former useAppState hook as a store factory.
* Returns { get(), subscribe(), destroy() } where get() exposes the same
* shape the hook returned (state fields + actions + the socket facade).
*/
export function createAppState() {
	const store = createStore({
		appVersion: "1.0.0",
		maintenanceReady: false,
		systemMetrics: {},
		confirmBusyId: null,
		switchBusy: null,
		confirmMessages: {}
	});
	const backendSocket = createBackendSocket();
	const sendCommand = backendSocket.sendCommand;
	const unsubscribers = [];

	const setMetricsIfChanged = (next) => {
		store.set((prev) => sameSystemMetrics(prev.systemMetrics, next) ? prev : { ...prev, systemMetrics: next });
	};
	const setConfirmMessage = (id, message) => {
		store.set((prev) => ({
			...prev,
			confirmMessages: { ...prev.confirmMessages, [id]: message }
		}));
	};
	const clearConfirmMessage = (id) => {
		store.set((prev) => {
			const next = { ...prev.confirmMessages };
			delete next[id];
			return { ...prev, confirmMessages: next };
		});
	};

	const waitForIpcEvent = (eventName, timeoutMs, predicate) => {
		return new Promise((resolve, reject) => {
			const timer = window.setTimeout(() => {
				window.removeEventListener("ipc_event", handler);
				reject(new Error(`${t.backendResponseTimeout}：${eventName}`));
			}, timeoutMs);
			const handler = (event) => {
				const customEvent = event;
				const detail = customEvent.detail || {};
				if (detail.event !== eventName) return;
				const payload = detail.payload || {};
				if (predicate && !predicate(payload)) return;
				window.clearTimeout(timer);
				window.removeEventListener("ipc_event", handler);
				resolve(payload);
			};
			window.addEventListener("ipc_event", handler);
		});
	};

	const confirmPendingAction = async (actionId) => {
		const state = store.get();
		if (!actionId || state.confirmBusyId) return;
		const action = (getRuntimeStatusState().pending_actions || []).find((item) => item.action_id === actionId);
		const kind = String(action?.kind || "").trim();
		if (kind !== "repair" && kind !== "update") {
			setConfirmMessage(actionId, xr.confirmedFailed);
			return;
		}
		const confirmCommand = `xingcheng-confirm-automatic-${kind}`;
		const executeCommand = `sync-execute-approved-automatic-${kind}`;
		store.merge({ confirmBusyId: actionId });
		try {
			const confirmSent = sendCommand(confirmCommand, {
				action_id: actionId,
				permission_mode: "single-item"
			});
			if (!confirmSent.ok) {
				setConfirmMessage(actionId, confirmSent.message || xr.confirmQueueFailed);
				return;
			}
			const confirmResult = await waitForIpcEvent(`${confirmCommand}_result`, 12e3, (payload) => !payload.action_id || String(payload.action_id) === actionId);
			if (confirmResult.ok !== true) {
				setConfirmMessage(actionId, String(confirmResult.message || "").trim() || xr.confirmedFailed);
				return;
			}
			const confirmationId = String(confirmResult.confirmation_id || "");
			const executeSent = sendCommand(executeCommand, {
				action_id: actionId,
				confirmation_id: confirmationId
			});
			if (!executeSent.ok) {
				setConfirmMessage(actionId, executeSent.message || xr.confirmQueueFailed);
				return;
			}
			const executeResult = await waitForIpcEvent(`${executeCommand}_result`, 12e4, (payload) => !payload.action_id || String(payload.action_id) === actionId);
			const ok = executeResult.ok === true;
			setConfirmMessage(actionId, ok ? xr.confirmedDone : String(executeResult.message || "").trim() || xr.confirmedFailed);
		} catch {
			setConfirmMessage(actionId, xr.confirmedFailed);
		} finally {
			store.merge({ confirmBusyId: null });
			sendCommand("app:get-runtime-status", { source: "pending_action_confirmation" });
		}
	};

	const denyPendingAction = async (actionId) => {
		if (!actionId || store.get().confirmBusyId) return;
		const command = "xingcheng-deny-pending-action";
		store.merge({ confirmBusyId: actionId });
		try {
			const sent = sendCommand(command, { action_id: actionId });
			if (!sent.ok) throw new Error(sent.message || xr.denyFailed);
			const result = await waitForIpcEvent(`${command}_result`, 12e3, (payload) => !payload.action_id || String(payload.action_id) === actionId);
			if (result.ok !== true) throw new Error(String(result.message || "") || xr.denyFailed);
			setConfirmMessage(actionId, xr.deniedDone);
		} catch {
			setConfirmMessage(actionId, xr.denyFailed);
		} finally {
			store.merge({ confirmBusyId: null });
			sendCommand("app:get-runtime-status", { source: "pending_action_denial" });
		}
	};

	const setAutomationSwitch = async (switchName, enabled) => {
		if (store.get().switchBusy) return;
		const command = switchName === "automatic_repair_enabled" ? "xingcheng-set-repair-release" : "xingcheng-set-update-release";
		store.merge({ switchBusy: switchName });
		try {
			const sent = sendCommand(command, { enabled });
			if (!sent.ok) throw new Error(sent.message || xr.switchSetFailed);
			const result = await waitForIpcEvent(`${command}_result`, 1e4);
			if (result.ok !== true) throw new Error(String(result.message || "") || xr.switchSetFailed);
			clearConfirmMessage(switchName);
		} catch {
			setConfirmMessage(switchName, xr.switchSetFailed);
		} finally {
			store.merge({ switchBusy: null });
			sendCommand("app:get-runtime-status", { source: "automation_switch_update" });
		}
	};

	const setXingchengNativeModelEnabled = async (enabled) => {
		const switchName = "xingcheng_native_model";
		if (store.get().switchBusy) return;
		store.merge({ switchBusy: switchName });
		try {
			const command = "xingcheng-set-native-model-enabled";
			const sent = sendCommand(command, { enabled });
			if (!sent.ok) throw new Error(sent.message || xr.nativeModelSetFailed);
			const result = await waitForIpcEvent(`${command}_result`, 13e4);
			if (result.ok !== true) throw new Error(String(result.message || "") || xr.nativeModelSetFailed);
			clearConfirmMessage(switchName);
		} catch (error) {
			setConfirmMessage(switchName, error instanceof Error ? error.message : xr.nativeModelSetFailed);
		} finally {
			store.merge({ switchBusy: null });
			sendCommand("app:get-runtime-status", { source: "xingcheng_native_model_update" });
		}
	};

	// --- mount-time effects -------------------------------------------------
	let disposed = false;

	const setupStatusChannel = () => {
		const api = window.electron;
		if (!api?.invoke) return;
		const refreshStatus = async () => {
			try {
				const status = await api.invoke("app:get-status");
				if (disposed) return;
				const version = String(status?.version ?? "").trim();
				if (version) store.merge({ appVersion: version });
				if (status?.systemMetrics) setMetricsIfChanged(status.systemMetrics);
			} catch {}
		};
		void refreshStatus();
		const onStatusPush = (event) => {
			const customEvent = event;
			const detail = customEvent.detail || {};
			if (detail.event !== "runtime_status_push") return;
			const payload = detail.payload || {};
			const version = String(payload.version ?? "").trim();
			if (version) store.merge({ appVersion: version });
			const metrics = payload.systemMetrics;
			if (metrics) setMetricsIfChanged(metrics);
		};
		window.addEventListener("ipc_event", onStatusPush);
		unsubscribers.push(() => window.removeEventListener("ipc_event", onStatusPush));
		const saved = (() => {
			try {
				const raw = window.localStorage.getItem(UI_ZOOM_STORAGE_KEY);
				const parsed = raw ? Number(raw) : 1;
				if (Number.isNaN(parsed) || parsed <= 0) return 1;
				return clampUiZoom(parsed);
			} catch {
				return 1;
			}
		})();
		void api.invoke("app:set-ui-zoom", { factor: saved });
	};

	const setupReadinessGate = () => {
		const onStatusPush = (event) => {
			if (disposed) return;
			const customEvent = event;
			const detail = customEvent.detail || {};
			if (detail.event !== "runtime_status_push" && detail.event !== "app:get-runtime-status_result") {
				return;
			}
			const payload = detail.payload || {};
			const ready = payload.maintenance_ready === true;
			store.merge({ maintenanceReady: ready });
			// Modular distribution: per-field subscribers update independently.
			applyRuntimeStatusReport(payload);
		};
		const applyConnected = () => {
			const connected = backendSocket.getState().status === "Connected";
			if (!connected) {
				store.merge({ maintenanceReady: false });
				return;
			}
			window.addEventListener("ipc_event", onStatusPush);
			const sent = sendCommand("app:get-runtime-status", { source: "product_readiness_gate" });
			if (!sent.ok) {
				store.merge({ maintenanceReady: false });
			}
		};
		let listenerAttached = false;
		const attach = () => {
			if (!listenerAttached) {
				listenerAttached = true;
				applyConnected();
			}
		};
		unsubscribers.push(() => window.removeEventListener("ipc_event", onStatusPush));
		unsubscribers.push(backendSocket.subscribe((state) => {
			if (state.status === "Connected") {
				attach();
			} else {
				listenerAttached = false;
				window.removeEventListener("ipc_event", onStatusPush);
				store.merge({ maintenanceReady: false });
			}
		}));
		applyConnected();
	};

	setupStatusChannel();
	if (serviceManager.getAllStates().length === 0) {
		void startStartupPipeline();
	}
	setupReadinessGate();

	return {
		get() {
			const state = store.get();
			const socketState = backendSocket.getState();
			const connected = socketState.status === "Connected";
			return {
				...state,
				backendSocket: socketState,
				sendCommand,
				connected,
				operational: connected && state.maintenanceReady,
				waitForIpcEvent,
				confirmPendingAction,
				denyPendingAction,
				setAutomationSwitch,
				setXingchengNativeModelEnabled
			};
		},
		subscribe(listener) {
			return store.subscribe(listener);
		},
		subscribeSocket(listener) {
			return backendSocket.subscribe(listener);
		},
		destroy() {
			disposed = true;
			for (const unsub of unsubscribers) unsub();
			backendSocket.destroy();
		}
	};
}
