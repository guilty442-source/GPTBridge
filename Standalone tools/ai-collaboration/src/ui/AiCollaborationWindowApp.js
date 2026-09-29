//! AiCollaborationWindowApp.js — ai-collaboration tool window
//! (React-free native ESM, E180/C116).
//!
//! Redesigned layout (same behaviour contract): a slim header
//! (brand + connection + global actions), an agent selection rail,
//! then a two-pane workspace — collaboration composer + response
//! feed on the left, embedded browser with its own toolbar on the
//! right.  Settings open as an overlay drawer.  State lives in a
//! store; subscriptions re-render the dynamic sections and patch
//! the static chrome in place.
import { createStore, h, rerender } from "../../../../shared-layer/src/ui/toolWindow/dom.js";
import { waitForIpcEvent } from "../../../../shared-layer/src/ui/toolWindow/toolWindowUtils.js";
import { createLocalBackendSocket } from "./backendSocket.js";
import { createEmbeddedBrowser } from "./useEmbeddedBrowser.js";
import { createCollabActions } from "./collabActions.js";
import {
	PROMPT_PRESETS,
	buildAgentRail,
	buildModeSegments,
	buildResponsesBody,
	buildSettings,
	buildTaskSelect,
	socketStatusLabel
} from "./collabView.js";
import "./ai-collaboration.css";

export function mountAiCollaborationWindowApp(root) {
	const socket = createLocalBackendSocket();
	const browser = createEmbeddedBrowser();

	const store = createStore({
		urlInput: "",
		agents: [],
		selectedAgents: new Set(),
		settingsOpen: false,
		newAgentName: "",
		newAgentProvider: "",
		newAgentUrl: "",
		draft: "",
		collabMode: "single",
		collabTasks: [],
		activeTaskId: "",
		message: "AI協作工具已就緒",
		messageId: "",
		responses: [],
		browserDrafts: {},
		busyAction: ""
	});
	let loadedSelection = false;
	let inflightRequest = "";

	const snapshot = () => {
		const s = store.get();
		return {
			...s,
			selectedAgentList: s.agents.filter((agent) => s.selectedAgents.has(agent.agent_id)),
			agentsById: new Map(s.agents.map((agent) => [agent.agent_id, agent])),
			activeTask: s.collabTasks.find((item) => String(item.task_id || "") === s.activeTaskId) || null
		};
	};

	// --- governed request helper -------------------------------------------
	const request = async (command, payload = {}, timeoutMs = 3e4) => {
		const requestId = String(payload.request_id || "").trim() || `${command}:${Date.now()}:${Math.random().toString(16).slice(2)}`;
		// Wait for the backend socket to finish connecting before sending —
		// prevents the "後端連線尚未就緒" failure during the initial loadState().
		try {
			await socket.waitUntilConnected(Math.min(timeoutMs, 15e3));
		} catch (error) {
			throw new Error(error instanceof Error ? error.message : "後端連線尚未就緒，指令未送出，請稍後再試。");
		}
		const waitPromise = waitForIpcEvent(`${command}_result`, timeoutMs,
			(p) => !requestId || String(p.request_id || "") === requestId);
		const sent = socket.sendCommand(command, {
			...payload,
			request_id: requestId
		});
		if (!sent.ok && !sent.queued) {
			const failure = await waitPromise;
			throw new Error(String(failure.message || sent.message || "送出指令失敗"));
		}
		return waitPromise;
	};

	const applyState = (state) => {
		const nextAgents = Array.isArray(state.agents) ? state.agents : [];
		const patch = { agents: nextAgents };
		if (Array.isArray(state.collab_tasks)) patch.collabTasks = state.collab_tasks;
		if (!loadedSelection && nextAgents.length > 0) {
			loadedSelection = true;
			patch.selectedAgents = new Set(nextAgents.filter((agent) => Number(agent.selected) === 1).map((agent) => agent.agent_id));
		}
		store.merge(patch);
	};

	const loadState = async (silent = false) => {
		try {
			const result = await request("ai_nexus_get_state", {}, 15e3);
			if (result.ok === false) throw new Error(String(result.message || "載入失敗"));
			applyState(result);
			const trackedId = store.get().messageId;
			if (trackedId && Array.isArray(result.messages)) {
				const tracked = result.messages.find((item) => String(item.message_id || "") === trackedId);
				if (tracked && Array.isArray(tracked.responses)) {
					store.merge({ responses: tracked.responses });
				}
			}
			const prev = store.get().message;
			if (!silent || /失敗|尚未就緒|逾時|正在載入/.test(prev)) {
				store.merge({ message: "AI協作工具已載入" });
			}
		} catch (error) {
			if (!silent) {
				store.merge({ message: error instanceof Error ? error.message : "載入 AI協作工具失敗" });
			}
		}
	};

	// --- actions -------------------------------------------------------------
	const acts = createCollabActions({
		store, snapshot, request, applyState, loadState, browser, socket,
	});

	// --- header --------------------------------------------------------------
	const connChip = h("span", { className: "ai-collab-conn", dataset: { state: "connecting" } },
		h("span", { className: "ai-collab-conn-dot" }),
		h("span", { className: "ai-collab-conn-label" }, socketStatusLabel(socket.getStatus())));
	const refreshBtn = h("button", {
		type: "button", className: "ai-collab-ghost", title: "重新整理狀態",
		onClick: () => void acts.loadState()
	}, "重新整理");
	const exportBtn = h("button", {
		type: "button", className: "ai-collab-ghost", title: "匯出診斷報告",
		onClick: () => void acts.exportReport()
	}, "匯出診斷");
	const settingsBtn = h("button", {
		type: "button", className: "ai-collab-ghost",
		"aria-expanded": "false",
		onClick: () => acts.setSettingsOpen(!store.get().settingsOpen)
	}, "設定");
	const header = h("header", { className: "ai-collab-header" },
		h("div", { className: "ai-collab-brand" },
			h("span", { className: "ai-collab-brand-mark" }, "◆"),
			h("span", { className: "ai-collab-brand-text" },
				h("strong", null, "AI 協作"),
				h("small", null, "最多六家外部 AI 協同"))),
		h("div", { className: "ai-collab-header-actions" },
			connChip, refreshBtn, exportBtn, settingsBtn));

	// --- agent rail + settings drawer ------------------------------------------
	const railHost = h("div", { className: "ai-collab-railhost" });
	const settingsHost = h("div", { className: "ai-collab-drawer-panel" });
	const drawerBackdrop = h("div", {
		className: "ai-collab-drawer-backdrop",
		onClick: () => acts.setSettingsOpen(false)
	});
	const drawer = h("div", { className: "ai-collab-drawer" },
		drawerBackdrop,
		h("div", { className: "ai-collab-drawer-inner" }, settingsHost));

	// --- composer --------------------------------------------------------------
	const modeSegHost = h("div");
	const agentSelect = h("select", {
		className: "ai-collab-agent-select", "aria-label": "單選協作 AI",
		onChange: (event) => void acts.selectSingleAgent(event.target.value)
	});
	const draftArea = h("textarea", {
		className: "ai-collab-draft",
		placeholder: "輸入要交給已勾選 AI 的協作需求",
		onInput: (event) => store.merge({ draft: event.target.value })
	});
	const sendBtn = h("button", {
		type: "button", className: "ai-collab-primary",
		onClick: () => void acts.sendGroupMessage()
	}, "送出協作");
	const cancelBtn = h("button", {
		type: "button", className: "ai-collab-ghost", style: { display: "none" },
		onClick: () => void acts.cancelSend()
	}, "取消");
	const messageLine = h("p", { className: "ai-collab-status", role: "status" }, store.get().message);
	const agentError = h("p", { className: "ai-collab-alert", role: "alert", style: { display: "none" } });
	const composerCard = h("section", { className: "ai-collab-card ai-collab-composer", "aria-label": "協作需求" },
		h("div", { className: "ai-collab-composer-top" },
			modeSegHost, agentSelect),
		h("div", { className: "ai-collab-presets" },
			PROMPT_PRESETS.map((preset) => h("button", {
				type: "button", className: "ai-collab-preset",
				onClick: () => acts.applyPromptPreset(preset.prompt)
			}, preset.label))),
		h("div", { className: "ai-collab-composer-main" },
			draftArea,
			h("div", { className: "ai-collab-composer-actions" },
				sendBtn, cancelBtn)),
		agentError, messageLine);

	// --- responses -------------------------------------------------------------
	const headHost = h("div", { className: "ai-collab-feed-head" });
	const responsesHost = h("div", { className: "ai-collab-feed-body" });
	const responsesCard = h("section", { className: "ai-collab-card ai-collab-feed", "aria-label": "AI 回應" },
		headHost, responsesHost);

	// --- browser pane ------------------------------------------------------------
	const browserProgress = h("div", { className: "ai-collab-progress" });
	const browserError = h("p", { className: "ai-collab-alert", role: "alert", style: { display: "none" } });
	const backBtn = h("button", { type: "button", className: "ai-collab-icon", "aria-label": "返回", title: "返回", onClick: () => void browser.goBack() }, "◀");
	const fwdBtn = h("button", { type: "button", className: "ai-collab-icon", "aria-label": "前進", title: "前進", onClick: () => void browser.goForward() }, "▶");
	const reloadBtn = h("button", { type: "button", className: "ai-collab-icon", "aria-label": "重新整理", title: "重新整理", onClick: () => void browser.reload() }, "⟳");
	const urlInputEl = h("input", {
		type: "text", className: "ai-collab-url-input",
		placeholder: "輸入網址，例如 https://chatgpt.com",
		onInput: (event) => store.merge({ urlInput: event.target.value })
	});
	const goBtn = h("button", { type: "submit", className: "ai-collab-primary" }, "前往");
	const rightPanel = h("div", { className: "ai-collab-browser-canvas", "aria-label": "內建瀏覽器網頁區" });
	const browserCard = h("section", { className: "ai-collab-card ai-collab-browser", "aria-label": "內建瀏覽器" },
		h("div", { className: "ai-collab-browser-bar" },
			backBtn, fwdBtn, reloadBtn,
			h("form", {
				className: "ai-collab-url-form",
				onSubmit: (e) => {
					e.preventDefault();
					handleNavigate(store.get().urlInput);
				}
			}, urlInputEl, goBtn)),
		browserProgress, browserError, rightPanel);

	const el = h("main", { className: "ai-collab-app" },
		header,
		railHost,
		drawer,
		h("div", { className: "ai-collab-workspace" },
			h("div", { className: "ai-collab-pane-left" },
				composerCard, responsesCard),
			browserCard));
	root.replaceChildren(el);

	// --- embedded-browser bounds ---------------------------------------------
	const browserBounds = () => {
		const rect = rightPanel.getBoundingClientRect();
		const width = Math.round(rect.width);
		const height = Math.round(rect.height);
		if (width < 1 || height < 1) return null;
		return { x: Math.round(rect.x), y: Math.round(rect.y), width, height };
	};
	// The canvas owns the embedded browser: valid bounds show it, a
	// collapsed or hidden panel detaches it.  Nothing is ever displayed
	// full-window.
	const syncBrowserBounds = async () => {
		const bounds = browserBounds();
		if (!bounds) {
			void browser.hideBrowser();
			return;
		}
		await browser.resize(bounds);
		void browser.showBrowser();
	};
	const handleNavigate = (url) => {
		store.merge({ urlInput: url });
		urlInputEl.value = url;
		const bounds = browserBounds();
		void browser.navigate(url, bounds ?? undefined).then(() => syncBrowserBounds());
	};
	const openProviderInBrowser = (agent) => {
		if (!agent) return;
		const targetUrl = agent.general_url || agent.home_url || "";
		if (!targetUrl) return;
		store.merge({ urlInput: targetUrl });
		urlInputEl.value = targetUrl;
		const bounds = browserBounds();
		void browser.openProvider(agent.agent_id, targetUrl, bounds ?? undefined).then(() => syncBrowserBounds());
	};

	// --- rendering ------------------------------------------------------------
	const patchChrome = () => {
		const s = snapshot();
		const b = browser.state();
		const busy = Boolean(s.busyAction);
		const conn = socket.getStatus();
		connChip.dataset.state = conn === "Connected" ? "ok" :
			conn === "Error" || conn === "Disconnected" ? "fail" : "connecting";
		connChip.querySelector(".ai-collab-conn-label").textContent =
			socketStatusLabel(conn);
		browserProgress.classList.toggle("is-active", b.loading);
		browserError.textContent = b.error;
		browserError.style.display = b.error ? "" : "none";
		const firstError = s.agents.find((agent) => agent.last_error)?.last_error;
		agentError.textContent = firstError || "";
		agentError.style.display = firstError ? "" : "none";
		backBtn.disabled = !b.canGoBack;
		fwdBtn.disabled = !b.canGoForward;
		reloadBtn.disabled = !b.sessionId;
		goBtn.disabled = !s.urlInput.trim();
		agentSelect.disabled = busy || s.agents.length === 0;
		draftArea.disabled = s.busyAction === "send";
		if (draftArea.value !== s.draft) draftArea.value = s.draft;
		sendBtn.disabled = busy || !s.draft.trim() || s.selectedAgents.size === 0;
		sendBtn.textContent = s.busyAction === "send" ? "協作中…" : "送出協作";
		cancelBtn.style.display = s.busyAction === "send" ? "" : "none";
		refreshBtn.disabled = busy;
		exportBtn.disabled = busy;
		exportBtn.textContent = s.busyAction === "export-report" ? "匯出中…" : "匯出診斷";
		settingsBtn.setAttribute("aria-expanded", String(s.settingsOpen));
		drawer.classList.toggle("is-open", s.settingsOpen);
		messageLine.textContent = s.message;
		// agent-select options follow the roster; the single-selection value
		// mirrors the checkbox set like the React controlled select did.
		const singleSel = s.selectedAgents.size === 1 ? Array.from(s.selectedAgents)[0] : "";
		agentSelect.replaceChildren(
			h("option", { value: "" }, s.selectedAgents.size === 0 ? "單選 AI…" : `已選 ${s.selectedAgents.size} 個 AI`),
			s.agents.map((agent) => h("option", { value: agent.agent_id }, agent.name)));
		agentSelect.value = singleSel;
	};
	const render = () => {
		const s = snapshot();
		rerender(railHost, () => buildAgentRail(s, acts));
		rerender(settingsHost, () => s.settingsOpen ? buildSettings(s, acts) : []);
		rerender(modeSegHost, () => buildModeSegments(s.collabMode,
			(mode) => store.merge({ collabMode: mode })));
		rerender(headHost, () => [
			h("div", { className: "ai-collab-feed-title" },
				h("strong", null, "協作結果"),
				h("span", { className: "ai-collab-count" },
					s.activeTask
						? String((s.activeTask.provider_results || []).length)
						: String(s.responses.length))),
			buildTaskSelect(s, acts)
		]);
		rerender(responsesHost, () => buildResponsesBody(s, acts));
		patchChrome();
	};

	const unsubStore = store.subscribe(render);
	const unsubSocket = socket.subscribeStatus(patchChrome);
	const unsubBrowser = browser.subscribe(patchChrome);

	// --- lifecycle -------------------------------------------------------------
	const resyncTimer = window.setInterval(() => {
		void loadState(true);
	}, 5e3);
	const onSocketConnected = (event) => {
		if (event.detail?.connected === true) void loadState(true);
	};
	window.addEventListener("socket_connected", onSocketConnected);

	let boundsMounted = true;
	const handleResize = () => {
		window.setTimeout(() => {
			if (boundsMounted) void syncBrowserBounds();
		}, 80);
	};
	const handleVisibility = () => {
		if (document.hidden) void browser.hideBrowser();
		else void syncBrowserBounds();
	};
	window.addEventListener("resize", handleResize);
	document.addEventListener("visibilitychange", handleVisibility);
	const boundsObserver = typeof ResizeObserver !== "undefined"
		? new ResizeObserver(() => {
			if (boundsMounted) void syncBrowserBounds();
		})
		: null;
	boundsObserver?.observe(rightPanel);

	render();
	void loadState();

	return {
		el,
		destroy() {
			boundsMounted = false;
			boundsObserver?.disconnect();
			window.removeEventListener("resize", handleResize);
			document.removeEventListener("visibilitychange", handleVisibility);
			window.removeEventListener("socket_connected", onSocketConnected);
			window.clearInterval(resyncTimer);
			unsubStore();
			unsubSocket();
			unsubBrowser();
			void browser.destroy();
			socket.destroy();
		}
	};
}
