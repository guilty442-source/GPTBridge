//! localBackendSocket.js — shared governed backend WebSocket client for
//! tool windows (React-free, E180/C116).  This is the shared-layer
//! counterpart of the retired per-tool ``useLocalBackendSocket`` hooks:
//! same loopback session validation, bounded exponential reconnect, and
//! ``ipc_event`` dispatch — but expressed as a plain factory so native
//! JS-ESM surfaces can consume it without React.
//!
//! Used by the independent-tool renderers (ai-assistant, ai-collaboration)
//! through their local ``backendSocket.js`` wrappers.
import { createStore } from "./dom.js";

const REQUIRED_PROTOCOL_VERSION = 1;
const RECONNECT_MAX_DELAY_MS = 8e3;
function mutationId(command) {
	const suffix = typeof globalThis.crypto?.randomUUID === "function" ? globalThis.crypto.randomUUID() : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
	return `${command}:${suffix}`;
}
function withIdempotencyKey(command, payload) {
	if (!payload || typeof payload !== "object" || Array.isArray(payload)) return payload;
	const record = payload;
	if (String(record.idempotency_key || "").trim()) return payload;
	return {
		...record,
		idempotency_key: String(record.operation_id || "").trim() || String(record.request_id || "").trim() || mutationId(command)
	};
}
function dispatchLocalFailure(command, payload, message) {
	const requestId = payload && typeof payload === "object" && !Array.isArray(payload) ? String(payload.request_id || "") : "";
	window.dispatchEvent(new CustomEvent("ipc_event", {
		detail: {
			event: `${command}_result`,
			payload: {
				ok: false,
				queued: false,
				message,
				request_id: requestId
			}
		}
	}));
}

/// Owns the tool-window ↔ governed backend WebSocket lifecycle.
/// Returns { getStatus, subscribeStatus, sendCommand, cancelQueuedCommands,
/// waitUntilConnected, destroy }.
export function createLocalBackendSocket({ closedMessage = "投資管家視窗已關閉，指令未送出。" } = {}) {
	const status = createStore("Disconnected");
	let socket = null;
	let reconnectTimer = null;
	let expectedPackageDigest = "";
	let connectGeneration = 0;
	let reconnectAttempt = 0;
	let disposed = false;
	const connectionWaiters = new Set();

	const setStatus = (s) => status.set(s);

	const waitUntilConnected = (timeoutMs = 15e3) => {
		if (socket?.readyState === WebSocket.OPEN) return Promise.resolve();
		return new Promise((resolve, reject) => {
			const waiter = {
				timer: 0,
				resolve: () => {
					window.clearTimeout(waiter.timer);
					connectionWaiters.delete(waiter);
					resolve();
				},
				reject: (error) => {
					window.clearTimeout(waiter.timer);
					connectionWaiters.delete(waiter);
					reject(error);
				}
			};
			waiter.timer = window.setTimeout(() => waiter.reject(new Error("後端連線等待逾時，指令未送出。")), Math.max(1e3, Math.min(6e4, timeoutMs)));
			connectionWaiters.add(waiter);
			if (socket?.readyState === WebSocket.OPEN) waiter.resolve();
		});
	};
	const cancelQueuedCommands = () => 0;
	const sendCommand = (command, payload = {}) => {
		const preparedPayload = withIdempotencyKey(command, payload);
		if (!socket || socket.readyState !== WebSocket.OPEN) {
			const message = "後端連線尚未就緒，指令未送出，請稍後再試。";
			dispatchLocalFailure(command, preparedPayload, message);
			return { ok: false, queued: false, message };
		}
		try {
			socket.send(JSON.stringify({ command, payload: preparedPayload }));
			return { ok: true, queued: false };
		} catch {
			const message = "WebSocket 傳送失敗，指令未送出。";
			dispatchLocalFailure(command, preparedPayload, message);
			return { ok: false, queued: false, message };
		}
	};

	const clearReconnectTimer = () => {
		if (reconnectTimer === null) return;
		window.clearTimeout(reconnectTimer);
		reconnectTimer = null;
	};
	const scheduleReconnect = () => {
		if (disposed || reconnectTimer !== null) return;
		const attempt = ++reconnectAttempt;
		const baseDelay = Math.min(RECONNECT_MAX_DELAY_MS, 500 * 2 ** (attempt - 1));
		const delay = Math.round(baseDelay + Math.random() * baseDelay * .2);
		reconnectTimer = window.setTimeout(() => {
			reconnectTimer = null;
			void connect();
		}, delay);
	};
	const ensureBackendStarted = async () => {
		const api = window.electron;
		if (!api?.invoke) throw new Error("Electron IPC bridge is unavailable");
		const result = await api.invoke("app:ensure-backend-started");
		if (!result || result.ok !== true) {
			throw new Error(String(result?.message || "Backend startup failed"));
		}
		expectedPackageDigest = String(result.packageDigest || "");
	};
	const backendWebSocketUrl = async () => {
		const api = window.electron;
		if (!api?.invoke) throw new Error("Electron IPC bridge is unavailable");
		const session = await api.invoke("app:get-backend-session");
		const websocketUrl = String(session?.websocketUrl || "");
		const parsed = new URL(websocketUrl);
		const port = Number(parsed.port);
		if (parsed.protocol !== "ws:" || parsed.hostname !== "127.0.0.1" || parsed.username || parsed.password || !Number.isInteger(port) || port < 1024 || port > 65535) {
			throw new Error("Backend session capability is unavailable");
		}
		if (window.gptBridge?.standaloneTool) {
			const packageDigest = String(session?.packageDigest || "");
			if (!/^[a-f0-9]{64}$/i.test(packageDigest)) {
				throw new Error("Backend package identity is unavailable");
			}
			if (expectedPackageDigest && packageDigest !== expectedPackageDigest) {
				throw new Error("Backend package identity changed during startup");
			}
			if (Number(session?.protocolVersion || 0) !== REQUIRED_PROTOCOL_VERSION) {
				throw new Error("Backend protocol version does not match this UI");
			}
			if (!String(session?.backendVersion || "").trim()) {
				throw new Error("Backend service version is unavailable");
			}
		}
		return websocketUrl;
	};
	const connect = async () => {
		if (disposed) return;
		if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) return;
		const generation = ++connectGeneration;
		setStatus("Connecting");
		try {
			await ensureBackendStarted();
			if (disposed || generation !== connectGeneration) return;
			const websocketUrl = await backendWebSocketUrl();
			if (disposed || generation !== connectGeneration) return;
			const ws = new WebSocket(websocketUrl);
			socket = ws;
			ws.onopen = () => {
				if (disposed || generation !== connectGeneration) {
					ws.close();
					return;
				}
				setStatus("Connected");
				reconnectAttempt = 0;
				clearReconnectTimer();
				for (const waiter of [...connectionWaiters]) waiter.resolve();
				window.dispatchEvent(new CustomEvent("socket_connected", { detail: { connected: true } }));
			};
			ws.onmessage = (event) => {
				if (disposed || socket !== ws) return;
				try {
					const frame = JSON.parse(String(event.data));
					if (typeof frame.event === "string") {
						window.dispatchEvent(new CustomEvent("ipc_event", { detail: {
							event: frame.event,
							payload: frame.payload
						} }));
					}
				} catch {}
			};
			ws.onerror = () => {
				if (!disposed && socket === ws) setStatus("Error");
				if (ws.readyState < WebSocket.CLOSING) ws.close();
			};
			ws.onclose = () => {
				if (socket === ws) socket = null;
				if (disposed || generation !== connectGeneration) return;
				setStatus("Disconnected");
				window.dispatchEvent(new CustomEvent("socket_connected", { detail: { connected: false } }));
				scheduleReconnect();
			};
		} catch (error) {
			if (disposed || generation !== connectGeneration) return;
			setStatus(`Error: ${error instanceof Error ? error.message : String(error)}`);
			scheduleReconnect();
		}
	};
	const reconnectNow = () => {
		if (document.visibilityState === "hidden") return;
		clearReconnectTimer();
		void connect();
	};
	window.addEventListener("online", reconnectNow);
	document.addEventListener("visibilitychange", reconnectNow);
	void connect();

	const destroy = () => {
		disposed = true;
		window.removeEventListener("online", reconnectNow);
		document.removeEventListener("visibilitychange", reconnectNow);
		connectGeneration += 1;
		reconnectAttempt = 0;
		clearReconnectTimer();
		for (const waiter of [...connectionWaiters]) {
			waiter.reject(new Error(closedMessage));
		}
		const ws = socket;
		socket = null;
		if (ws && ws.readyState < WebSocket.CLOSING) ws.close();
	};

	return {
		getStatus: status.get,
		subscribeStatus: status.subscribe,
		cancelQueuedCommands,
		sendCommand,
		waitUntilConnected,
		destroy
	};
}
