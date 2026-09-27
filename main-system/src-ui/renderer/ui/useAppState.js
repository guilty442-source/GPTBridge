import { useCallback, useEffect, useState } from "react";
import { serviceManager, startStartupPipeline } from "@/services/RuntimeServiceManager";
import { useBackendSocket } from "@/hooks/useBackendSocket";
import { mainSystemLocale } from "@/locales/main-system";
import { applyRuntimeStatusReport, getRuntimeStatusState } from "@/shared/services/runtimeStatusStore";
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
function setMetricsIfChanged(setMetrics, next) {
	setMetrics((prev) => sameSystemMetrics(prev, next) ? prev : next);
}
export function useAppState() {
	const [appVersion, setAppVersion] = useState("1.0.0");
	const [maintenanceReady, setMaintenanceReady] = useState(false);
	const [systemMetrics, setSystemMetrics] = useState({});
	const [confirmBusyId, setConfirmBusyId] = useState(null);
	const [switchBusy, setSwitchBusy] = useState(null);
	const [confirmMessages, setConfirmMessages] = useState({});
	const backendSocket = useBackendSocket();
	const sendCommand = backendSocket.sendCommand;
	const connected = backendSocket.status === "Connected";
	const operational = connected && maintenanceReady;
	const waitForIpcEvent = useCallback((eventName, timeoutMs, predicate) => {
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
	}, []);
	const confirmPendingAction = useCallback(async (actionId) => {
		if (!actionId || confirmBusyId) return;
		const action = (getRuntimeStatusState().pending_actions || []).find((item) => item.action_id === actionId);
		const kind = String(action?.kind || "").trim();
		if (kind !== "repair" && kind !== "update") {
			setConfirmMessages((prev) => ({
				...prev,
				[actionId]: xr.confirmedFailed
			}));
			return;
		}
		const confirmCommand = `xingcheng-confirm-automatic-${kind}`;
		const executeCommand = `sync-execute-approved-automatic-${kind}`;
		setConfirmBusyId(actionId);
		try {
			const confirmSent = sendCommand(confirmCommand, {
				action_id: actionId,
				permission_mode: "single-item"
			});
			if (!confirmSent.ok) {
				setConfirmMessages((prev) => ({
					...prev,
					[actionId]: confirmSent.message || xr.confirmQueueFailed
				}));
				return;
			}
			const confirmResult = await waitForIpcEvent(`${confirmCommand}_result`, 12e3, (payload) => !payload.action_id || String(payload.action_id) === actionId);
			if (confirmResult.ok !== true) {
				setConfirmMessages((prev) => ({
					...prev,
					[actionId]: String(confirmResult.message || "").trim() || xr.confirmedFailed
				}));
				return;
			}
			const confirmationId = String(confirmResult.confirmation_id || "");
			const executeSent = sendCommand(executeCommand, {
				action_id: actionId,
				confirmation_id: confirmationId
			});
			if (!executeSent.ok) {
				setConfirmMessages((prev) => ({
					...prev,
					[actionId]: executeSent.message || xr.confirmQueueFailed
				}));
				return;
			}
			const executeResult = await waitForIpcEvent(`${executeCommand}_result`, 12e4, (payload) => !payload.action_id || String(payload.action_id) === actionId);
			const ok = executeResult.ok === true;
			setConfirmMessages((prev) => ({
				...prev,
				[actionId]: ok ? xr.confirmedDone : String(executeResult.message || "").trim() || xr.confirmedFailed
			}));
		} catch {
			setConfirmMessages((prev) => ({
				...prev,
				[actionId]: xr.confirmedFailed
			}));
		} finally {
			setConfirmBusyId(null);
			sendCommand("app:get-runtime-status", { source: "pending_action_confirmation" });
		}
	}, [
		confirmBusyId,
		sendCommand,
		waitForIpcEvent
	]);
	const denyPendingAction = useCallback(async (actionId) => {
		if (!actionId || confirmBusyId) return;
		const command = "xingcheng-deny-pending-action";
		setConfirmBusyId(actionId);
		try {
			const sent = sendCommand(command, { action_id: actionId });
			if (!sent.ok) throw new Error(sent.message || xr.denyFailed);
			const result = await waitForIpcEvent(`${command}_result`, 12e3, (payload) => !payload.action_id || String(payload.action_id) === actionId);
			if (result.ok !== true) throw new Error(String(result.message || "") || xr.denyFailed);
			setConfirmMessages((prev) => ({
				...prev,
				[actionId]: xr.deniedDone
			}));
		} catch {
			setConfirmMessages((prev) => ({
				...prev,
				[actionId]: xr.denyFailed
			}));
		} finally {
			setConfirmBusyId(null);
			sendCommand("app:get-runtime-status", { source: "pending_action_denial" });
		}
	}, [
		confirmBusyId,
		sendCommand,
		waitForIpcEvent
	]);
	const setAutomationSwitch = useCallback(async (switchName, enabled) => {
		if (switchBusy) return;
		const command = switchName === "automatic_repair_enabled" ? "xingcheng-set-repair-release" : "xingcheng-set-update-release";
		setSwitchBusy(switchName);
		try {
			const sent = sendCommand(command, { enabled });
			if (!sent.ok) throw new Error(sent.message || xr.switchSetFailed);
			const result = await waitForIpcEvent(`${command}_result`, 1e4);
			if (result.ok !== true) throw new Error(String(result.message || "") || xr.switchSetFailed);
			setConfirmMessages((prev) => {
				const next = { ...prev };
				delete next[switchName];
				return next;
			});
		} catch {
			setConfirmMessages((prev) => ({
				...prev,
				[switchName]: xr.switchSetFailed
			}));
		} finally {
			setSwitchBusy(null);
			sendCommand("app:get-runtime-status", { source: "automation_switch_update" });
		}
	}, [
		switchBusy,
		sendCommand,
		waitForIpcEvent
	]);
	const setXingchengNativeModelEnabled = useCallback(async (enabled) => {
		const switchName = "xingcheng_native_model";
		if (switchBusy) return;
		setSwitchBusy(switchName);
		try {
			const command = "xingcheng-set-native-model-enabled";
			const sent = sendCommand(command, { enabled });
			if (!sent.ok) throw new Error(sent.message || xr.nativeModelSetFailed);
			const result = await waitForIpcEvent(`${command}_result`, 13e4);
			if (result.ok !== true) throw new Error(String(result.message || "") || xr.nativeModelSetFailed);
			setConfirmMessages((prev) => {
				const next = { ...prev };
				delete next[switchName];
				return next;
			});
		} catch (error) {
			setConfirmMessages((prev) => ({
				...prev,
				[switchName]: error instanceof Error ? error.message : xr.nativeModelSetFailed
			}));
		} finally {
			setSwitchBusy(null);
			sendCommand("app:get-runtime-status", { source: "xingcheng_native_model_update" });
		}
	}, [
		switchBusy,
		sendCommand,
		waitForIpcEvent
	]);
	useEffect(() => {
		const api = window.electron;
		if (!api?.invoke) return;
		let disposed = false;
		const refreshStatus = async () => {
			try {
				const status = await api.invoke("app:get-status");
				if (disposed) return;
				const version = String(status?.version ?? "").trim();
				if (version) setAppVersion(version);
				if (status?.systemMetrics) setMetricsIfChanged(setSystemMetrics, status.systemMetrics);
			} catch {}
		};
		void refreshStatus();
		const onStatusPush = (event) => {
			const customEvent = event;
			const detail = customEvent.detail || {};
			if (detail.event !== "runtime_status_push") return;
			const payload = detail.payload || {};
			const version = String(payload.version ?? "").trim();
			if (version) setAppVersion(version);
			const metrics = payload.systemMetrics;
			if (metrics) setMetricsIfChanged(setSystemMetrics, metrics);
		};
		window.addEventListener("ipc_event", onStatusPush);
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
		return () => {
			disposed = true;
			window.removeEventListener("ipc_event", onStatusPush);
		};
	}, []);
	useEffect(() => {
		if (serviceManager.getAllStates().length === 0) {
			void startStartupPipeline();
		}
	}, []);
	useEffect(() => {
		let disposed = false;
		if (!connected) {
			setMaintenanceReady(false);
			return () => undefined;
		}
		const onStatusPush = (event) => {
			if (disposed) return;
			const customEvent = event;
			const detail = customEvent.detail || {};
			if (detail.event !== "runtime_status_push" && detail.event !== "app:get-runtime-status_result") {
				return;
			}
			const payload = detail.payload || {};
			const ready = payload.maintenance_ready === true;
			setMaintenanceReady(ready);
			// Modular distribution: per-field subscribers update independently.
			applyRuntimeStatusReport(payload);
		};
		window.addEventListener("ipc_event", onStatusPush);
		const sent = sendCommand("app:get-runtime-status", { source: "product_readiness_gate" });
		if (!sent.ok) {
			setMaintenanceReady(false);
		}
		return () => {
			disposed = true;
			window.removeEventListener("ipc_event", onStatusPush);
		};
	}, [connected, sendCommand]);
	return {
		appVersion,
		maintenanceReady,
		systemMetrics,
		confirmBusyId,
		switchBusy,
		confirmMessages,
		backendSocket,
		sendCommand,
		connected,
		operational,
		waitForIpcEvent,
		confirmPendingAction,
		denyPendingAction,
		setAutomationSwitch,
		setXingchengNativeModelEnabled
	};
}
