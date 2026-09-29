//! AiCollaborationWindowApp.js — ai-collaboration tool window
//! (React-free native ESM, E180/C116).
//!
//! Same behaviour as the retired React app: one integrated top card
//! (URL toolbar, AI roster, selection, settings, collaboration input)
//! above a two-column workspace (AI responses | embedded browser).
//! State lives in a store; subscriptions re-render the dynamic sections
//! and patch the static chrome in place.
import { createStore, h, rerender } from "../../../../shared-layer/src/ui/toolWindow/dom.js";
import { waitForIpcEvent } from "../../../../shared-layer/src/ui/toolWindow/toolWindowUtils.js";
import { createLocalBackendSocket } from "./backendSocket.js";
import { createEmbeddedBrowser } from "./useEmbeddedBrowser.js";
import { createCollabActions } from "./collabActions.js";
import {
	COLLAB_MODES,
	PROMPT_PRESETS,
	buildAgentRow,
	buildResponsesBody,
	buildSettings,
	buildTaskSelect,
	responseText,
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

	// --- static skeleton -----------------------------------------------------
	const statusChip = h("span", { className: "ai-collab-chip" }, socketStatusLabel(socket.getStatus()));
	const loadingChip = h("span", { className: "ai-collab-chip ai-collab-chip--running", style: { display: "none" } }, "載入中");
	const browserError = h("p", { className: "ai-collab-error", role: "alert", style: { display: "none" } });
	const agentError = h("p", { className: "ai-collab-error", role: "alert", style: { display: "none" } });
	const urlInputEl = h("input", {
		type: "text", className: "ai-collab-url-input",
		placeholder: "輸入網址，例如 https://chatgpt.com",
		onInput: (event) => store.merge({ urlInput: event.target.value })
	});
	const backBtn = h("button", { type: "button", "aria-label": "返回", title: "返回", onClick: () => void browser.goBack() }, "◀");
	const fwdBtn = h("button", { type: "button", "aria-label": "前進", title: "前進", onClick: () => void browser.goForward() }, "▶");
	const reloadBtn = h("button", { type: "button", "aria-label": "重新整理", title: "重新整理", onClick: () => void browser.reload() }, "⟳");
	const goBtn = h("button", { type: "submit", className: "ai-collab-primary" }, "前往");
	const urlRow = h("div", { className: "ai-collab-topcard-row ai-collab-urlrow" },
		backBtn, fwdBtn, reloadBtn,
		h("form", {
			className: "ai-collab-url-form",
			onSubmit: (e) => {
				e.preventDefault();
				handleNavigate(store.get().urlInput);
			}
		}, urlInputEl, goBtn),
		statusChip, loadingChip);

	const agentRowHost = h("div");
	const settingsHost = h("div");

	const modeSelect = h("select", {
		className: "ai-collab-mode-select", "aria-label": "協作模式",
		onChange: (event) => store.merge({ collabMode: event.target.value })
	}, COLLAB_MODES.map((mode) => h("option", { value: mode.id, selected: mode.id === store.get().collabMode }, mode.label)));
	const agentSelect = h("select", {
		className: "ai-collab-agent-select", "aria-label": "選擇協作 AI",
		onChange: (event) => void acts.selectSingleAgent(event.target.value)
	});
	const draftArea = h("textarea", {
		placeholder: "輸入要交給已勾選 AI 的協作需求",
		onInput: (event) => store.merge({ draft: event.target.value })
	});
	const sendBtn = h("button", {
		type: "button", className: "ai-collab-primary",
		onClick: () => void acts.sendGroupMessage()
	}, "送出協作");
	const cancelBtn = h("button", {
		type: "button", style: { display: "none" },
		onClick: () => void acts.cancelSend()
	}, "取消");
	const composerRow = h("div", { className: "ai-collab-topcard-row ai-collab-composerrow", role: "group", "aria-label": "協作需求輸入" },
		modeSelect, agentSelect,
		PROMPT_PRESETS.map((preset) => h("button", {
			type: "button", className: "ai-collab-preset",
			onClick: () => acts.applyPromptPreset(preset.prompt)
		}, preset.label)),
		draftArea, sendBtn, cancelBtn);
	const messageLine = h("p", { className: "ai-collab-muted", role: "status" }, store.get().message);

	const headHost = h("div", { className: "ai-collab-top-agents-head" });
	const responsesHost = h("div");
	const rightPanel = h("div", { className: "ai-collab-right", "aria-label": "內建瀏覽器網頁區" },
		h("div", { className: "ai-collab-browser-canvas" }));

	const el = h("main", { className: "ai-collab-app" },
		h("section", { className: "ai-collab-topcard", role: "group", "aria-label": "外部協作整合區" },
			urlRow, browserError, agentRowHost, agentError, settingsHost, composerRow, messageLine),
		h("section", { className: "ai-collab-workspace", "aria-label": "外部協作雙欄工作區" },
			h("div", { className: "ai-collab-left", role: "region", "aria-label": "AI 協作結果" },
				h("div", { className: "ai-collab-responses", role: "group", "aria-label": "AI 回應" },
					headHost, responsesHost)),
			rightPanel));
	root.replaceChildren(el);

	// --- embedded-browser bounds ---------------------------------------------
	const browserBounds = () => {
		const rect = rightPanel.getBoundingClientRect();
		const width = Math.round(rect.width);
		const height = Math.round(rect.height);
		if (width < 1 || height < 1) return null;
		return { x: Math.round(rect.x), y: Math.round(rect.y), width, height };
	};
	// The panel owns the embedded browser: valid bounds show it, a collapsed
	// or hidden panel detaches it.  Nothing is ever displayed full-window.
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
		statusChip.textContent = socketStatusLabel(socket.getStatus());
		loadingChip.style.display = b.loading ? "" : "none";
		browserError.textContent = b.error;
		browserError.style.display = b.error ? "" : "none";
		const firstError = s.agents.find((agent) => agent.last_error)?.last_error;
		agentError.textContent = firstError || "";
		agentError.style.display = firstError ? "" : "none";
		backBtn.disabled = !b.canGoBack;
		fwdBtn.disabled = !b.canGoForward;
		reloadBtn.disabled = !b.sessionId;
		goBtn.disabled = !s.urlInput.trim();
		modeSelect.disabled = busy;
		agentSelect.disabled = busy || s.agents.length === 0;
		draftArea.disabled = s.busyAction === "send";
		if (draftArea.value !== s.draft) draftArea.value = s.draft;
		sendBtn.disabled = busy || !s.draft.trim() || s.selectedAgents.size === 0;
		sendBtn.textContent = s.busyAction === "send" ? "協作中..." : "送出協作";
		cancelBtn.style.display = s.busyAction === "send" ? "" : "none";
		messageLine.textContent = s.message;
		// agent-select options follow the roster; the single-selection value
		// mirrors the checkbox set like the React controlled select did.
		const singleSel = s.selectedAgents.size === 1 ? Array.from(s.selectedAgents)[0] : "";
		agentSelect.replaceChildren(
			h("option", { value: "" }, s.selectedAgents.size === 0 ? "選擇 AI…" : `已選 ${s.selectedAgents.size} 個 AI`),
			s.agents.map((agent) => h("option", { value: agent.agent_id }, agent.name)));
		agentSelect.value = singleSel;
	};
	const render = () => {
		const s = snapshot();
		rerender(agentRowHost, () => buildAgentRow(s, acts));
		rerender(settingsHost, () => s.settingsOpen ? buildSettings(s, acts) : []);
		rerender(headHost, () => [
			h("div", null,
				h("span", null, "AI 回應"),
				h("strong", null, s.activeTask ? String((s.activeTask.provider_results || []).length) : String(s.responses.length))),
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
