import { BootLogger } from "../BootLogger.js";
import { eventBus } from "../RuntimeEventBus.js";
import { getAuthenticatedBackendWebSocketUrl } from "./backendSession.js";
import { resetBackendRecovery } from "./backendRecovery.js";
import { mainSystemLocale } from "@/locales/main-system";
import { INITIAL_STATE, WS_RECONNECT_BASE_DELAY_MS, WS_RECONNECT_MAX_DELAY_MS, WS_CONNECT_TIMEOUT_MS, WS_COMMAND_QUEUE_MAX, WS_COMMAND_QUEUE_TTL_MS, WS_STALE_CONNECTION_MS, OUTBOX_CURSOR_KEY, OUTBOX_GENERATION_KEY, updateBackendConnectionSnapshot } from "./backendSocketTypes.js";
import { handleOutboxSession, handleOutboxStateEvent } from "./backendSocketOutbox.js";
import { applyRuntimeStatusReport } from "./runtimeStatusStore.js";
import { createStore } from "../mini/dom.js";
export { getBackendConnectionSnapshot } from "./backendSocketTypes.js";

/**
* createBackendSocket — the governed loopback WebSocket client formerly
* exposed as the useBackendSocket React hook.  Same contract: reconnect
* with exponential backoff+jitter, command queue with TTL, authenticated
* session URL via the preload invoke, transactional outbox cursor
* tracking (A195), heartbeat pong, stale-connection close, and the
* ipc_event window-event fanout every panel subscribes to.
*
* Returns { getState(), subscribe(), sendCommand(), destroy() }.
*/
export const createBackendSocket = () => {
	const store = createStore({ ...INITIAL_STATE });
	let lastError = null;
	let socket = null;
	let reconnectTimer = null;
	let connectTimeout = null;
	let staleConnectionTimer = null;
	let reconnectAttempt = 0;
	const commandQueue = [];
	let commandQueueHead = 0;
	let lastMessageAt = 0;
	// A195 transactional outbox client state: applied cursor survives socket
	// reconnects within the same window session (RECONNECT sends last-acked
	// cursor; gap → scoped invalidation + replay).
	const outboxAppliedRef = { current: Number(window.sessionStorage.getItem(OUTBOX_CURSOR_KEY) || 0) || 0 };
	const outboxBufferRef = { current: new Map() };
	// Generation reported by the current backend session.  Events stamped
	// with any other (superseded) generation are stale backlog replay and
	// must never be applied — doing so used to trigger a status-request
	// storm and exhaust the command rate limit.
	const sessionGenerationRef = { current: null };
	const ws = mainSystemLocale.websocket;

	const setLastStatusAt = () => {
		store.set((prev) => {
			const now = Date.now();
			if (typeof prev.lastStatusAt === "number" && now - prev.lastStatusAt < 1e3) {
				return prev;
			}
			return { ...prev, lastStatusAt: now };
		});
	};

	const flushCommandQueue = () => {
		const head = commandQueueHead;
		if (head >= commandQueue.length) return;
		if (!socket || socket.readyState !== WebSocket.OPEN) return;
		while (commandQueueHead < commandQueue.length) {
			const item = commandQueue[commandQueueHead];
			if (Date.now() - item.queuedAt > WS_COMMAND_QUEUE_TTL_MS) {
				BootLogger.log("WebSocket", "QUEUE_ITEM_EXPIRED", { command: item.command }, "warn");
				commandQueueHead += 1;
				continue;
			}
			try {
				socket.send(JSON.stringify({
					command: item.command,
					payload: item.payload
				}));
				BootLogger.log("WebSocket", "QUEUE_FLUSH", { command: item.command });
				commandQueueHead += 1;
			} catch {
				break;
			}
		}
		const nextHead = commandQueueHead;
		if (nextHead === commandQueue.length) {
			commandQueue.length = 0;
			commandQueueHead = 0;
		} else if (nextHead > 32) {
			commandQueue.splice(0, nextHead);
			commandQueueHead = 0;
		}
	};

	const sendCommand = (command, payload = {}) => {
		if (!socket || socket.readyState !== WebSocket.OPEN) {
			// Queue the command for later flush instead of dropping it
			if (command !== "heartbeat_pong") {
				if (commandQueue.length - commandQueueHead < WS_COMMAND_QUEUE_MAX) {
					commandQueue.push({
						command,
						payload,
						queuedAt: Date.now()
					});
					BootLogger.log("WebSocket", "COMMAND_QUEUED", {
						command,
						queueSize: commandQueue.length
					});
					store.merge({ queuedCommands: commandQueue.length - commandQueueHead });
					return {
						ok: false,
						queued: true,
						message: ws.autoFlush
					};
				}
			}
			const errorMsg = ws.notReady;
			lastError = errorMsg;
			BootLogger.log("WebSocket", "SEND_REJECTED_OFFLINE", { command }, "warn");
			return {
				ok: false,
				queued: false,
				message: errorMsg
			};
		}
		try {
			socket.send(JSON.stringify({
				command,
				payload
			}));
			BootLogger.log("WebSocket", "SEND", { command });
			return {
				ok: true,
				queued: false
			};
		} catch {
			const errorMsg = "WebSocket closed before the command was sent; command was queued";
			// Queue for retry
			if (command !== "heartbeat_pong") {
				if (commandQueue.length - commandQueueHead < WS_COMMAND_QUEUE_MAX) {
					commandQueue.push({
						command,
						payload,
						queuedAt: Date.now()
					});
				}
			}
			return {
				ok: false,
				queued: true,
				message: errorMsg
			};
		}
	};

	let disposed = false;
	const clearReconnectTimer = () => {
		if (reconnectTimer !== null) {
			window.clearTimeout(reconnectTimer);
			reconnectTimer = null;
		}
	};
	const clearConnectTimeout = () => {
		if (connectTimeout !== null) {
			window.clearTimeout(connectTimeout);
			connectTimeout = null;
		}
	};
	const clearStaleConnectionTimer = () => {
		if (staleConnectionTimer !== null) {
			window.clearInterval(staleConnectionTimer);
			staleConnectionTimer = null;
		}
	};
	const scheduleReconnect = () => {
		if (disposed) return;
		clearReconnectTimer();
		const nextAttempt = reconnectAttempt + 1;
		reconnectAttempt = nextAttempt;
		// Exponential backoff with jitter: 500ms, 1s, 2s, 4s, 8s (max)
		// + up to 20% jitter to avoid thundering herd
		const baseDelay = Math.min(WS_RECONNECT_MAX_DELAY_MS, WS_RECONNECT_BASE_DELAY_MS * Math.pow(2, nextAttempt - 1));
		const jitter = Math.random() * baseDelay * .2;
		const delay = Math.round(baseDelay + jitter);
		reconnectTimer = window.setTimeout(() => {
			reconnectTimer = null;
			void (async () => {
				await connect();
			})();
		}, delay);
		BootLogger.log("WebSocket", "RECONNECT_SCHEDULED", {
			attempt: nextAttempt,
			delay
		});
		// Do NOT request a backend restart on reconnect exhaustion.
		// The backend process is likely still running — only the WebSocket
		// connection dropped. A backend restart is disruptive and should
		// only be triggered by a verified startup_dead condition (checked
		// in applyRuntimeReadiness), not by transient WebSocket failures.
	};
	const connect = async () => {
		if (disposed) return;
		if (socket && (socket.readyState === WebSocket.OPEN || socket.readyState === WebSocket.CONNECTING)) {
			return;
		}
		store.merge({ status: "Connecting" });
		updateBackendConnectionSnapshot("Connecting");
		let wsUrl = "";
		try {
			wsUrl = await getAuthenticatedBackendWebSocketUrl();
		} catch (error) {
			const message = error instanceof Error ? error.message : String(error);
			lastError = message;
			updateBackendConnectionSnapshot("Error");
			store.merge({ status: "Error", lastError: message });
			BootLogger.log("WebSocket", "SESSION_FAILED", { error: message }, "error");
			scheduleReconnect();
			return;
		}
		if (disposed) return;
		const endpointLabel = (() => {
			try {
				return new URL(wsUrl).host;
			} catch {
				return "backend";
			}
		})();
		BootLogger.log("WebSocket", "CONNECTING", { endpoint: endpointLabel });
		const current = new WebSocket(wsUrl);
		socket = current;
		clearConnectTimeout();
		connectTimeout = window.setTimeout(() => {
			if (current.readyState === WebSocket.CONNECTING) current.close();
		}, WS_CONNECT_TIMEOUT_MS);
		const applyRuntimeReadiness = (runtime) => {
			const ready = runtime.ok === true && runtime.runtime_state === "ready" && runtime.governance_ready === true && runtime.startup_dead !== true;
			if (ready) {
				reconnectAttempt = 0;
				resetBackendRecovery();
				updateBackendConnectionSnapshot("Connected", current, true);
				store.merge({ status: "Connected", reconnectAttempt: 0 });
				eventBus.emit("socket_connected", { connected: true });
				flushCommandQueue();
				store.merge({ queuedCommands: commandQueue.length - commandQueueHead });
			} else {
				// No polling: the backend pushes a fresh report every cycle and on
				// every readiness change, so this state converges without requests.
				updateBackendConnectionSnapshot("Synchronizing", current, false);
				store.merge({ status: "Synchronizing" });
				eventBus.emit("socket_connected", { connected: false });
				// A195/A196: a startup_dead payload means the
				// backend PROCESS is alive (it just sent us a message) but its
				// runtime init failed — the governed recovery owner is boot_core,
				// not the UI socket.  Surface a typed degraded signal instead.
				if (runtime.startup_dead === true) {
					eventBus.emit("backend_startup_dead", { runtime_state: runtime.runtime_state });
					BootLogger.log("WebSocket", "BACKEND_STARTUP_DEAD_DEGRADED", {}, "warn");
				}
			}
		};
		current.onopen = () => {
			clearConnectTimeout();
			clearReconnectTimer();
			lastError = null;
			lastMessageAt = Date.now();
			updateBackendConnectionSnapshot("Synchronizing");
			store.merge({
				status: "Synchronizing",
				lastStatusAt: Date.now(),
				reconnectAttempt,
				queuedCommands: commandQueue.length - commandQueueHead
			});
			BootLogger.log("WebSocket", "OPEN", { endpoint: endpointLabel });
			// The backend owns the refresh: it sends an immediate health report
			// on connection plus a report every cycle.  The client never polls.
			// A195 RECONNECT: resubscribe to the transactional outbox with the
			// last acknowledged cursor so the backend replays missed events.
			try {
				current.send(JSON.stringify({
					command: "state_event_hello",
					payload: {
						cursor: outboxAppliedRef.current,
						generation: window.sessionStorage.getItem(OUTBOX_GENERATION_KEY) || ""
					}
				}));
			} catch {}
		};
		current.onmessage = (event) => {
			lastMessageAt = Date.now();
			try {
				const payload = JSON.parse(String(event.data));
				if (payload.event === "app:get-runtime-status_result" || payload.event === "runtime_status_push") {
					const runtime = payload.payload ?? {};
					applyRuntimeReadiness(runtime);
					// Modular distribution: each module subscribes to its own field.
					applyRuntimeStatusReport(runtime);
				}
				// A195: outbox session answer — a generation change means the
				// backend restarted; invalidate the projection and resume the
				// stream from sequence 0 instead of trusting the stale cursor.
				if (payload.event === "state_event_session") {
					handleOutboxSession(payload.payload, {
						outboxAppliedRef,
						outboxBufferRef,
						sessionGenerationRef
					});
				}
				// A195: transactional outbox state event — validate sequence,
				// apply once, acknowledge the contiguous cursor.
				if (payload.event === "state_event") {
					handleOutboxStateEvent(payload.payload, {
						outboxAppliedRef,
						outboxBufferRef,
						sessionGenerationRef
					}, (command, p) => {
						try {
							current.send(JSON.stringify({
								command,
								payload: p
							}));
						} catch {}
					}, () => undefined);
				}
				// Respond to heartbeat ping immediately
				if (payload.event === "heartbeat_ping") {
					try {
						current.send(JSON.stringify({
							command: "heartbeat_pong",
							payload: {}
						}));
					} catch {}
				}
				if (payload.event && typeof payload.event === "string") {
					eventBus.emit(payload.event, payload.payload);
					window.dispatchEvent(new CustomEvent("ipc_event", { detail: {
						event: payload.event,
						payload: payload.payload
					} }));
				}
				// Perf/low-render: lastStatusAt has no per-packet readers;
				// 1s granularity avoids a full re-render on every WS packet.
				setLastStatusAt();
				eventBus.emit("backend_message", payload);
			} catch (error) {
				const message = error instanceof Error ? error.message : String(error);
				BootLogger.log("WebSocket", "PARSE_ERROR", { error: message }, "error");
			}
		};
		current.onerror = () => {
			const errorMsg = ws.autoRepairing;
			lastError = errorMsg;
			updateBackendConnectionSnapshot("Error");
			store.merge({ status: "Error", lastError: errorMsg });
			BootLogger.log("WebSocket", "ERROR", {}, "error");
			if (current.readyState < WebSocket.CLOSING) current.close();
		};
		current.onclose = () => {
			clearConnectTimeout();
			if (socket === current) {
				socket = null;
			}
			const snapshot = updateBackendConnectionSnapshot("Disconnected", current, false);
			store.merge({ status: "Disconnected", lastError: ws.autoReconnecting });
			BootLogger.log("WebSocket", "CLOSED");
			eventBus.emit("socket_connected", { connected: snapshot.connected });
			if (!disposed) {
				scheduleReconnect();
			}
		};
	};
	const reconnectNow = () => {
		if (document.visibilityState === "hidden") return;
		clearReconnectTimer();
		clearConnectTimeout();
		void connect();
	};
	window.addEventListener("online", reconnectNow);
	document.addEventListener("visibilitychange", reconnectNow);
	// Perf/low-CPU: stale threshold is 25s; 10s sampling still detects
	// within ~35s worst case at one-third of the wakeups.
	// idle-ok: O(1) local timestamp check with no IPC, and closing a
	// dead socket while hidden frees the backend connection early.
	staleConnectionTimer = window.setInterval(() => {
		if (socket?.readyState === WebSocket.OPEN && lastMessageAt > 0 && Date.now() - lastMessageAt > WS_STALE_CONNECTION_MS) {
			BootLogger.log("WebSocket", "STALE_CONNECTION_CLOSED", {}, "warn");
			socket.close(4e3, "stale-connection");
		}
	}, 1e4);
	void connect();

	return {
		getState() {
			const state = store.get();
			return {
				...state,
				sendCommand,
				lastError: lastError ?? state.lastError
			};
		},
		subscribe(listener) {
			return store.subscribe(listener);
		},
		sendCommand,
		destroy() {
			disposed = true;
			window.removeEventListener("online", reconnectNow);
			document.removeEventListener("visibilitychange", reconnectNow);
			clearReconnectTimer();
			clearConnectTimeout();
			clearStaleConnectionTimer();
			reconnectAttempt = 0;
			commandQueue.length = 0;
			commandQueueHead = 0;
			const current = socket;
			socket = null;
			if (current) {
				updateBackendConnectionSnapshot("Disconnected", current, false);
			}
			if (current && current.readyState < WebSocket.CLOSING) {
				current.close();
			}
		}
	};
};
