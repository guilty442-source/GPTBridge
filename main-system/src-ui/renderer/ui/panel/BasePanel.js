import { createPanelDrawer } from "./PanelDrawer.js";
import { mainSystemLocale } from "@/locales/main-system";
import { h, hs } from "@/shared/mini/dom.js";
import "./BasePanel.css";
const t = mainSystemLocale.toolbox;

/**
* createBasePanel — former BasePanel.jsx.  Provides the governed panel
* chrome (drawer + error banner + disconnected notice) and the ctx object
* panel contents consumed: {state, locale, send, waitForEvent,
* generateRequestId, setLoading, setError, clearError, connected, loading}.
*
* opts: {onClose, title, eyebrow, icon, side, headerActions, content(ctx)}
* where content(ctx) -> {el, update?()|null, destroy?()|null}.
* ``getConnected()`` supplies the live socket status getter so panels see
* reconnects (former backendSocket.status prop change).
*
* Returns {setOpen(open), ctx(), destroy()}.
*/
export function createBasePanel({ onClose, title, eyebrow, icon, side = "right", headerActions, content, getConnected }) {
	const state = {
		loading: false,
		error: "",
		connected: Boolean(getConnected?.())
	};
	let errorEl = null;
	let disconnectedEl = null;
	let mountedContent = null;

	const locale = {
		launch: t.start,
		launching: t.starting,
		stop: t.stop,
		closing: t.stopping,
		status: t.status,
		statusStopped: t.statusStopped,
		statusRunning: t.statusRunning,
		statusError: t.statusError,
		errorFetch: t.errorFetch,
		errorTimeout: t.errorTimeout,
		disconnected: t.disconnected,
		refresh: t.refresh,
		loading: t.loading
	};

	const setLoading = (loading) => {
		state.loading = loading;
	};
	const setError = (error) => {
		state.error = error;
		syncChrome();
	};
	const clearError = () => {
		state.error = "";
		syncChrome();
	};
	const send = (command, payload) => sendCommandRef(command, payload);
	const waitForEvent = (eventName, timeoutMs = 3e4, predicate) => waitForIpcEventRef(eventName, timeoutMs, predicate);
	const generateRequestId = (prefix = "panel") => `${prefix}:${Date.now()}:${Math.random().toString(16).slice(2)}`;

	// sendCommand/waitForIpcEvent arrive via bind(); indirection keeps the
	// ctx stable across open cycles like the React prop drilling did.
	let sendCommandRef = () => ({ ok: false, queued: false, message: "" });
	let waitForIpcEventRef = async () => ({});

	const syncChrome = () => {
		if (!errorEl) return;
		if (state.error) {
			errorEl.style.display = "";
			errorEl.firstChild.textContent = state.error;
		} else {
			errorEl.style.display = "none";
		}
	};

	const setConnected = (connected) => {
		state.connected = connected;
		if (disconnectedEl) {
			disconnectedEl.style.display = connected ? "none" : "";
		}
		mountedContent?.update?.();
	};

	const ctx = {
		get state() { return state; },
		locale,
		send,
		waitForEvent,
		generateRequestId,
		setLoading,
		setError,
		clearError,
		get connected() { return state.connected; },
		get loading() { return state.loading; }
	};

	const drawer = createPanelDrawer({
		onClose,
		title,
		eyebrow,
		icon,
		side,
		headerActions,
		render: (body) => {
			errorEl = h("div", { className: "base-panel__error", role: "alert", style: { display: "none" } },
				h("span", null),
				h("button", {
					type: "button",
					className: "base-panel__error-dismiss",
					onClick: () => clearError(),
					"aria-label": mainSystemLocale.common.close
				},
					hs("svg", { width: "14", height: "14", viewBox: "0 0 14 14", fill: "none" },
						hs("path", { d: "M4 4L10 10M10 4L4 10", stroke: "currentColor", "stroke-width": "1.5", "stroke-linecap": "round" }))));
			disconnectedEl = h("div", {
				className: "base-panel__disconnected",
				style: { display: state.connected ? "none" : "" }
			}, locale.disconnected);
			const panel = h("div", { className: "base-panel" }, errorEl, disconnectedEl);
			body.appendChild(panel);
			mountedContent = content?.(ctx) || null;
			if (mountedContent?.el) panel.appendChild(mountedContent.el);
			syncChrome();
		}
	});

	return {
		setOpen(open) {
			if (open) {
				state.error = "";
				state.loading = false;
			} else {
				mountedContent?.destroy?.();
				mountedContent = null;
			}
			drawer.setOpen(open);
		},
		isOpen() {
			return drawer.isOpen();
		},
		setConnected,
		bind(sendCommand, waitForIpcEvent) {
			sendCommandRef = sendCommand;
			waitForIpcEventRef = waitForIpcEvent;
		},
		ctx() {
			return ctx;
		},
		destroy() {
			mountedContent?.destroy?.();
			drawer.destroy();
		}
	};
}
