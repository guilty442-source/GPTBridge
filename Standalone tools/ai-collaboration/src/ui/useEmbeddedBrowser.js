//! useEmbeddedBrowser.js — embedded-browser session controller
//! (React-free, E180/C116).  Same contract as the retired hook: owns the
//! host-side browser session lifecycle through the preload ``invoke``
//! bridge, tracks navigation state from ``embedded-browser:event``
//! host events, and exposes an observable ``state`` store plus the
//! navigation/visibility actions.
import { createStore } from "../../../../shared-layer/src/ui/toolWindow/dom.js";
function getElectron() {
	const api = window.electron;
	return api ?? null;
}
const OWNER_MODULE = "ai-collaboration";
const FREE_SESSION_ID = `${OWNER_MODULE}:browser`;
export function providerSessionId(agentId) {
	return `${OWNER_MODULE}-${agentId}`;
}
export function createEmbeddedBrowser() {
	const store = createStore({
		sessionId: null,
		currentUrl: "",
		loading: false,
		error: "",
		canGoBack: false,
		canGoForward: false
	});
	let sessionId = null;
	let previousSessionId = null;
	const invoke = async (channel, ...args) => {
		const electron = getElectron();
		if (!electron) throw new Error("Electron IPC bridge is unavailable");
		return electron.invoke(channel, ...args);
	};
	const refreshState = async () => {
		const id = sessionId;
		if (!id) return;
		try {
			const result = await invoke("embedded-browser:state", { id });
			if (!result.ok || sessionId !== id) return;
			store.merge({
				currentUrl: String(result.url || store.get().currentUrl || ""),
				loading: Boolean(result.loading),
				canGoBack: Boolean(result.canGoBack),
				canGoForward: Boolean(result.canGoForward)
			});
		} catch {}
	};
	// Real navigation lifecycle events from the host — drives the loading
	// indicator, keeps the URL bar in sync and surfaces page errors.
	const unsubscribe = getElectron()?.onEvent?.("embedded-browser:event", (payload) => {
		const detail = payload;
		const id = String(detail.id || "");
		if (!id || id !== sessionId) return;
		const type = String(detail.type || "");
		if (type === "loading-start") {
			store.merge({ loading: true, error: "" });
		} else if (type === "loading-stop") {
			store.merge({ loading: false });
			void refreshState();
		} else if (type === "navigate" || type === "navigate-in-page") {
			const url = String(detail.url || "");
			store.merge({ currentUrl: url });
			void refreshState();
		} else if (type === "load-failed") {
			store.merge({
				loading: false,
				error: String(detail.error || "") || `載入失敗 (${String(detail.errorCode || "")})`
			});
		}
	});
	const activateSession = async (id, url, bounds) => {
		const result = await invoke("embedded-browser:create", {
			id,
			ownerModule: OWNER_MODULE,
			url,
			bounds
		});
		if (!result.ok) throw new Error(result.message || "無法建立瀏覽器");
		const previous = previousSessionId !== id ? sessionId : null;
		previousSessionId = sessionId;
		sessionId = result.id || id;
		if (previous && previous !== sessionId) {
			void invoke("embedded-browser:hide", { id: previous }).catch(() => {});
		}
		store.merge({
			sessionId,
			currentUrl: url || store.get().currentUrl,
			error: ""
		});
		await refreshState();
		return sessionId;
	};
	const navigate = async (rawUrl, bounds) => {
		const url = normalizeUrl(rawUrl);
		if (!url) {
			store.merge({ error: "請輸入有效的網址" });
			return;
		}
		const existingId = sessionId;
		store.merge({ loading: true, error: "", currentUrl: url });
		try {
			if (existingId) {
				await invoke("embedded-browser:navigate", {
					id: existingId,
					url
				});
				if (bounds) {
					await invoke("embedded-browser:resize", {
						id: existingId,
						bounds
					});
				}
			} else {
				await activateSession(FREE_SESSION_ID, url, bounds);
			}
			store.merge({ sessionId, loading: true });
		} catch (error) {
			store.merge({
				loading: false,
				error: error instanceof Error ? error.message : "瀏覽器操作失敗"
			});
		}
	};
	const openProvider = async (agentId, url, bounds) => {
		const target = normalizeUrl(url) || url;
		store.merge({ loading: true, error: "", currentUrl: target });
		try {
			await activateSession(providerSessionId(agentId), target, bounds);
		} catch (error) {
			store.merge({
				loading: false,
				error: error instanceof Error ? error.message : "瀏覽器操作失敗"
			});
		}
	};
	const showBrowser = async () => {
		const id = sessionId;
		if (!id) return;
		try {
			await invoke("embedded-browser:show", { id });
		} catch {}
	};
	const hideBrowser = async () => {
		const id = sessionId;
		if (!id) return;
		try {
			await invoke("embedded-browser:hide", { id });
		} catch {}
	};
	const closeBrowser = async () => {
		const id = sessionId;
		if (!id) return;
		try {
			await invoke("embedded-browser:close", { id });
		} catch {}
		sessionId = null;
		store.set({
			sessionId: null,
			currentUrl: "",
			loading: false,
			error: "",
			canGoBack: false,
			canGoForward: false
		});
	};
	const reload = async () => {
		const id = sessionId;
		if (!id) return;
		store.merge({ loading: true, error: "" });
		try {
			await invoke("embedded-browser:reload", { id });
		} catch {
			store.merge({ loading: false });
		}
	};
	const goBack = async () => {
		const id = sessionId;
		if (!id) return;
		try {
			await invoke("embedded-browser:go-back", { id });
			void refreshState();
		} catch {}
	};
	const goForward = async () => {
		const id = sessionId;
		if (!id) return;
		try {
			await invoke("embedded-browser:go-forward", { id });
			void refreshState();
		} catch {}
	};
	const getUrl = async () => {
		const id = sessionId;
		if (!id) return null;
		try {
			const result = await invoke("embedded-browser:url", { id });
			return result.ok ? result.url || null : null;
		} catch {
			return null;
		}
	};
	const resize = async (bounds) => {
		const id = sessionId;
		if (!id) return;
		try {
			await invoke("embedded-browser:resize", {
				id,
				bounds
			});
		} catch {}
	};
	const destroy = () => {
		if (typeof unsubscribe === "function") unsubscribe();
		const id = sessionId;
		if (!id) return;
		void invoke("embedded-browser:hide", { id }).catch(() => {});
	};
	return {
		state: store.get,
		subscribe: store.subscribe,
		navigate,
		openProvider,
		showBrowser,
		hideBrowser,
		closeBrowser,
		resize,
		reload,
		goBack,
		goForward,
		getUrl,
		refreshState,
		destroy
	};
}
function normalizeUrl(input) {
	const trimmed = input.trim();
	if (!trimmed) return "";
	if (/^https?:\/\//i.test(trimmed)) return trimmed;
	if (/^[\w-]+(\.[\w-]+)+/.test(trimmed)) return `https://${trimmed}`;
	return "";
}
