//! hooks.js — ai-assistant workspace IPC helpers (React-free, E180/C116).
//! Plain-function replacements for the retired ``useIpcEvent`` /
//! ``useIpcEvents`` / ``useCommandQuery`` hooks; each returns an
//! unsubscribe/destroy handle so a page switch never leaks a
//! subscription (memory-bounded contract).
import { createStore } from "../../../../../shared-layer/src/ui/toolWindow/dom.js";

/// Subscribe to a backend ``ipc_event`` name.  Returns an unsubscribe
/// function — callers MUST invoke it on destroy.
export function onIpcEvent(eventName, handler) {
	const listener = (e) => {
		const detail = e.detail;
		if (detail?.event === eventName) handler(detail.payload);
	};
	window.addEventListener("ipc_event", listener);
	return () => window.removeEventListener("ipc_event", listener);
}
/// Subscribe to every ``ipc_event``; handler decides relevance.
export function onIpcEvents(handler) {
	const listener = (e) => {
		const detail = e.detail;
		if (detail?.event) handler(String(detail.event), detail.payload);
	};
	window.addEventListener("ipc_event", listener);
	return () => window.removeEventListener("ipc_event", listener);
}
/**
 * Command query: sends ``command`` through the governed WS channel and
 * captures the matching ``<command>_result`` frame into a store of
 * {data, loading, error, empty, refreshedAt}.  Returns
 * {get, subscribe, refresh, destroy} — ``refresh()`` re-sends the
 * command, ``destroy()`` detaches the ipc listener.
 */
export function createCommandQuery(sendCommand, command, payload = {}, isEmpty) {
	const store = createStore({
		data: undefined,
		loading: true,
		error: "",
		empty: false,
		refreshedAt: 0
	});
	const unsub = onIpcEvent(`${command}_result`, (raw) => {
		const p = raw;
		store.set({
			data: p,
			loading: false,
			error: p && p.ok === false ? String(p.error_code || p.message || "BACKEND_ERROR") : "",
			empty: p != null && isEmpty ? isEmpty(p) : false,
			refreshedAt: Date.now()
		});
	});
	let currentPayload = payload;
	const fire = () => {
		store.merge({ loading: true, error: "" });
		const ack = sendCommand(command, currentPayload);
		if (!ack.ok) {
			store.merge({ loading: false, error: ack.message || "指令未送出" });
		}
	};
	fire();
	return {
		get: store.get,
		subscribe: store.subscribe,
		refresh: fire,
		/// Re-send the command with a new payload (the retired hook's
		/// ``deps``-rerun contract).
		setPayload: (next) => {
			currentPayload = next;
			fire();
		},
		destroy: unsub
	};
}
