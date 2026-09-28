import { useCallback, useEffect, useRef, useState } from "react";
function getElectron() {
	const api = window.electron;
	return api ?? null;
}
const OWNER_MODULE = "ai-collaboration";
const FREE_SESSION_ID = `${OWNER_MODULE}:browser`;
export function providerSessionId(agentId) {
	return `${OWNER_MODULE}-${agentId}`;
}
export function useEmbeddedBrowser() {
	const [state, setState] = useState({
		sessionId: null,
		currentUrl: "",
		loading: false,
		error: "",
		canGoBack: false,
		canGoForward: false
	});
	const sessionRef = useRef(null);
	const previousSessionRef = useRef(null);
	const invoke = useCallback(async (channel, ...args) => {
		const electron = getElectron();
		if (!electron) throw new Error("Electron IPC bridge is unavailable");
		return electron.invoke(channel, ...args);
	}, []);
	const refreshState = useCallback(async () => {
		const id = sessionRef.current;
		if (!id) return;
		try {
			const result = await invoke("embedded-browser:state", { id });
			if (!result.ok || sessionRef.current !== id) return;
			setState((prev) => ({
				...prev,
				currentUrl: String(result.url || prev.currentUrl || ""),
				loading: Boolean(result.loading),
				canGoBack: Boolean(result.canGoBack),
				canGoForward: Boolean(result.canGoForward)
			}));
		} catch {}
	}, [invoke]);
	// Real navigation lifecycle events from the host — drives the loading
	// indicator, keeps the URL bar in sync and surfaces page errors.
	useEffect(() => {
		const electron = getElectron();
		const unsubscribe = electron?.onEvent?.("embedded-browser:event", (payload) => {
			const detail = payload;
			const id = String(detail.id || "");
			if (!id || id !== sessionRef.current) return;
			const type = String(detail.type || "");
			if (type === "loading-start") {
				setState((prev) => ({
					...prev,
					loading: true,
					error: ""
				}));
			} else if (type === "loading-stop") {
				setState((prev) => ({
					...prev,
					loading: false
				}));
				void refreshState();
			} else if (type === "navigate" || type === "navigate-in-page") {
				const url = String(detail.url || "");
				setState((prev) => ({
					...prev,
					currentUrl: url
				}));
				void refreshState();
			} else if (type === "load-failed") {
				setState((prev) => ({
					...prev,
					loading: false,
					error: String(detail.error || "") || `載入失敗 (${String(detail.errorCode || "")})`
				}));
			}
		});
		return () => {
			if (typeof unsubscribe === "function") unsubscribe();
		};
	}, [refreshState]);
	const activateSession = useCallback(async (id, url, bounds) => {
		const result = await invoke("embedded-browser:create", {
			id,
			ownerModule: OWNER_MODULE,
			url,
			bounds
		});
		if (!result.ok) throw new Error(result.message || "無法建立瀏覽器");
		const previous = previousSessionRef.current !== id ? sessionRef.current : null;
		previousSessionRef.current = sessionRef.current;
		sessionRef.current = result.id || id;
		if (previous && previous !== sessionRef.current) {
			void invoke("embedded-browser:hide", { id: previous }).catch(() => {});
		}
		setState((prev) => ({
			...prev,
			sessionId: sessionRef.current,
			currentUrl: url || prev.currentUrl,
			error: ""
		}));
		await refreshState();
		return sessionRef.current;
	}, [invoke, refreshState]);
	const navigate = useCallback(async (rawUrl, bounds) => {
		const url = normalizeUrl(rawUrl);
		if (!url) {
			setState((prev) => ({
				...prev,
				error: "請輸入有效的網址"
			}));
			return;
		}
		const existingId = sessionRef.current;
		setState((prev) => ({
			...prev,
			loading: true,
			error: "",
			currentUrl: url
		}));
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
			setState((prev) => ({
				...prev,
				sessionId: sessionRef.current,
				loading: true
			}));
		} catch (error) {
			setState((prev) => ({
				...prev,
				loading: false,
				error: error instanceof Error ? error.message : "瀏覽器操作失敗"
			}));
		}
	}, [activateSession, invoke]);
	const openProvider = useCallback(async (agentId, url, bounds) => {
		const target = normalizeUrl(url) || url;
		setState((prev) => ({
			...prev,
			loading: true,
			error: "",
			currentUrl: target
		}));
		try {
			await activateSession(providerSessionId(agentId), target, bounds);
		} catch (error) {
			setState((prev) => ({
				...prev,
				loading: false,
				error: error instanceof Error ? error.message : "瀏覽器操作失敗"
			}));
		}
	}, [activateSession]);
	const showBrowser = useCallback(async () => {
		const id = sessionRef.current;
		if (!id) return;
		try {
			await invoke("embedded-browser:show", { id });
		} catch {}
	}, [invoke]);
	const hideBrowser = useCallback(async () => {
		const id = sessionRef.current;
		if (!id) return;
		try {
			await invoke("embedded-browser:hide", { id });
		} catch {}
	}, [invoke]);
	const closeBrowser = useCallback(async () => {
		const id = sessionRef.current;
		if (!id) return;
		try {
			await invoke("embedded-browser:close", { id });
		} catch {}
		sessionRef.current = null;
		setState({
			sessionId: null,
			currentUrl: "",
			loading: false,
			error: "",
			canGoBack: false,
			canGoForward: false
		});
	}, [invoke]);
	const reload = useCallback(async () => {
		const id = sessionRef.current;
		if (!id) return;
		setState((prev) => ({
			...prev,
			loading: true,
			error: ""
		}));
		try {
			await invoke("embedded-browser:reload", { id });
		} catch {
			setState((prev) => ({
				...prev,
				loading: false
			}));
		}
	}, [invoke]);
	const goBack = useCallback(async () => {
		const id = sessionRef.current;
		if (!id) return;
		try {
			await invoke("embedded-browser:go-back", { id });
			void refreshState();
		} catch {}
	}, [invoke, refreshState]);
	const goForward = useCallback(async () => {
		const id = sessionRef.current;
		if (!id) return;
		try {
			await invoke("embedded-browser:go-forward", { id });
			void refreshState();
		} catch {}
	}, [invoke, refreshState]);
	const getUrl = useCallback(async () => {
		const id = sessionRef.current;
		if (!id) return null;
		try {
			const result = await invoke("embedded-browser:url", { id });
			return result.ok ? result.url || null : null;
		} catch {
			return null;
		}
	}, [invoke]);
	const resize = useCallback(async (bounds) => {
		const id = sessionRef.current;
		if (!id) return;
		try {
			await invoke("embedded-browser:resize", {
				id,
				bounds
			});
		} catch {}
	}, [invoke]);
	useEffect(() => {
		return () => {
			const id = sessionRef.current;
			if (!id) return;
			void invoke("embedded-browser:hide", { id }).catch(() => {});
		};
	}, [invoke]);
	return {
		state,
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
		refreshState
	};
}
function normalizeUrl(input) {
	const trimmed = input.trim();
	if (!trimmed) return "";
	if (/^https?:\/\//i.test(trimmed)) return trimmed;
	if (/^[\w-]+(\.[\w-]+)+/.test(trimmed)) return `https://${trimmed}`;
	return "";
}
