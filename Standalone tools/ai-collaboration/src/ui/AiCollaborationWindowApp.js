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
	const acts = {
		loadState: () => loadState(),
		setField: (field, value) => store.merge({ [field]: value }),
		setSettingsOpen: (open) => store.merge({ settingsOpen: open }),
		setBrowserDraft: (key, value) => store.merge({ browserDrafts: { ...store.get().browserDrafts, [key]: value } }),
		setActiveTaskId: (id) => store.merge({ activeTaskId: id }),
		toggleAgent: async (agentId) => {
			const next = new Set(store.get().selectedAgents);
			if (next.has(agentId)) next.delete(agentId); else next.add(agentId);
			store.merge({ selectedAgents: next });
			try {
				const result = await request("ai_nexus_set_agent_selection", { agent_ids: Array.from(next) });
				if (Array.isArray(result.agents)) store.merge({ agents: result.agents });
			} catch (error) {
				store.merge({ message: error instanceof Error ? error.message : "儲存 AI 名單失敗" });
			}
		},
		selectSingleAgent: async (agentId) => {
			if (!agentId) return;
			store.merge({ selectedAgents: new Set([agentId]) });
			try {
				const result = await request("ai_nexus_set_agent_selection", { agent_ids: [agentId] });
				if (Array.isArray(result.agents)) store.merge({ agents: result.agents });
			} catch (error) {
				store.merge({ message: error instanceof Error ? error.message : "儲存 AI 名單失敗" });
			}
		},
		openAgent: async (agentId) => {
			store.merge({ busyAction: `open:${agentId}` });
			try {
				const agent = snapshot().agentsById.get(agentId);
				const result = await request("ai_nexus_open_agent", { agent_id: agentId, business_scope: "general" }, 3e4);
				if (result.ok === false) throw new Error(String(result.message || "開啟失敗"));
				openProviderInBrowser(agent);
				store.merge({ message: `${agentId} 已在內建瀏覽器開啟` });
				await loadState(true);
			} catch (error) {
				store.merge({ message: error instanceof Error ? error.message : "開啟 AI 失敗" });
			} finally {
				store.merge({ busyAction: "" });
			}
		},
		authorizeAgent: async (agentId) => {
			store.merge({ busyAction: `authorize:${agentId}` });
			try {
				const agent = snapshot().agentsById.get(agentId);
				const result = await request("ai_nexus_authorize_agent", { agent_id: agentId }, 3e4);
				if (result.ok === false) throw new Error(String(result.message || "啟動授權失敗"));
				openProviderInBrowser(agent);
				store.merge({ message: `${agentId} 已在內建瀏覽器開啟；請完成一次登入或人機驗證` });
				await loadState(true);
			} catch (error) {
				store.merge({ message: error instanceof Error ? error.message : "啟動帳號授權失敗" });
			} finally {
				store.merge({ busyAction: "" });
			}
		},
		openSelectedAgents: async () => {
			const s = snapshot();
			if (s.selectedAgents.size === 0) {
				store.merge({ message: "請至少選擇一個 AI" });
				return;
			}
			store.merge({ busyAction: "open-selected" });
			try {
				const result = await request("ai_nexus_open_selected_agents", {
					agent_ids: Array.from(s.selectedAgents),
					business_scope: "general"
				}, 6e4);
				if (result.ok === false) throw new Error(String(result.message || "開啟選取 AI 失敗"));
				applyState(result);
				const firstSelected = snapshot().selectedAgentList[0];
				if (firstSelected) openProviderInBrowser(firstSelected);
				store.merge({ message: String(result.message || "已開啟選取 AI") });
			} catch (error) {
				store.merge({ message: error instanceof Error ? error.message : "開啟選取 AI 失敗" });
			} finally {
				store.merge({ busyAction: "" });
			}
		},
		exportReport: async () => {
			store.merge({ busyAction: "export-report" });
			try {
				const result = await request("ai_nexus_export_report", {}, 1e4);
				if (result.ok === false) throw new Error(String(result.message || "診斷報告匯出失敗"));
				const reportPath = String(result.report_path || "");
				store.merge({ message: reportPath ? `診斷報告已匯出：${reportPath}` : "診斷報告已匯出" });
			} catch (error) {
				store.merge({ message: error instanceof Error ? error.message : "診斷報告匯出失敗" });
			} finally {
				store.merge({ busyAction: "" });
			}
		},
		updateAgentSetting: (agentId, field, value) => {
			store.merge({
				agents: store.get().agents.map((agent) => agent.agent_id === agentId ? { ...agent, [field]: value } : agent)
			});
		},
		saveAgentBusinessSettings: async (agent) => {
			store.merge({ busyAction: `settings:${agent.agent_id}` });
			try {
				const result = await request("ai_nexus_update_agent_business_settings", {
					agent_id: agent.agent_id,
					general_url: agent.general_url,
					investment_url: agent.investment_url,
					star_training_url: agent.star_training_url,
					general_enabled: Boolean(agent.general_enabled),
					investment_enabled: Boolean(agent.investment_enabled),
					business_capabilities: agent.business_capabilities
				});
				if (result.ok === false) throw new Error(String(result.message || "設定儲存失敗"));
				const patch = { message: String(result.message || "業務 URL 已儲存") };
				if (Array.isArray(result.agents)) patch.agents = result.agents;
				store.merge(patch);
			} catch (error) {
				store.merge({ message: error instanceof Error ? error.message : "業務 URL 儲存失敗" });
			} finally {
				store.merge({ busyAction: "" });
			}
		},
		addAgent: async () => {
			const s = store.get();
			if (!s.newAgentName.trim()) {
				store.merge({ message: "請輸入 AI 名稱" });
				return;
			}
			if (!s.newAgentUrl.trim()) {
				store.merge({ message: "請輸入 AI 網址" });
				return;
			}
			store.merge({ busyAction: "add-agent" });
			try {
				const result = await request("ai_nexus_add_agent", {
					name: s.newAgentName.trim(),
					provider: s.newAgentProvider.trim(),
					home_url: s.newAgentUrl.trim()
				});
				if (result.ok === false) throw new Error(String(result.message || "新增 AI 失敗"));
				const patch = {
					newAgentName: "", newAgentProvider: "", newAgentUrl: "",
					message: String(result.message || "已新增 AI")
				};
				if (Array.isArray(result.agents)) patch.agents = result.agents;
				store.merge(patch);
			} catch (error) {
				store.merge({ message: error instanceof Error ? error.message : "新增 AI 失敗" });
			} finally {
				store.merge({ busyAction: "" });
			}
		},
		applyPromptPreset: (prompt) => {
			const trimmed = store.get().draft.trim();
			store.merge({ draft: trimmed ? `${trimmed}\n\n${prompt}` : prompt });
		},
		sendGroupMessage: async () => {
			const s = snapshot();
			if (!s.draft.trim()) {
				store.merge({ message: "請輸入要交給 AI 協作的內容" });
				return;
			}
			if (s.selectedAgents.size === 0) {
				store.merge({ message: "請至少選擇一個 AI" });
				return;
			}
			const selectedProviders = s.selectedAgentList.map((agent) => agent.provider);
			if (s.collabMode !== "single" && selectedProviders.length < 2) {
				store.merge({ message: "此模式至少需要兩個 AI" });
				return;
			}
			const requestId = `ai_nexus_collab_start:${Date.now()}:${Math.random().toString(16).slice(2)}`;
			inflightRequest = requestId;
			store.merge({ busyAction: "send", message: `正在交給 ${selectedProviders.length} 個 AI 協作...` });
			try {
				const result = await request("ai_nexus_collab_start", {
					content: s.draft,
					provider_ids: selectedProviders,
					mode: s.collabMode,
					request_id: requestId,
					idempotency_key: requestId
				}, 3e5);
				if (result.ok === false) throw new Error(String(result.message || "送出失敗"));
				const task = result.task || {};
				const nextTaskId = String(task.task_id || "");
				const patch = { draft: "", message: String(result.message || "AI 協作已完成") };
				if (nextTaskId) patch.activeTaskId = nextTaskId;
				if (Array.isArray(result.collab_tasks)) patch.collabTasks = result.collab_tasks;
				if (Array.isArray(result.agents)) patch.agents = result.agents;
				store.merge(patch);
			} catch (error) {
				store.merge({ message: error instanceof Error ? error.message : "AI 協作送出失敗" });
			} finally {
				inflightRequest = "";
				store.merge({ busyAction: "" });
			}
		},
		cancelSend: async () => {
			const requestId = inflightRequest;
			const taskId = store.get().activeTaskId;
			store.merge({ message: "正在取消協作請求..." });
			try {
				if (taskId) {
					await request("ai_nexus_collab_cancel", { task_id: taskId }, 15e3);
				} else if (requestId) {
					socket.sendCommand("toolbox_cancel_tool_run", { request_id: requestId });
				}
				await loadState(true);
			} catch {}
		},
		submitBrowserResult: async (providerId) => {
			const s = store.get();
			const taskId = s.activeTaskId;
			if (!taskId) {
				store.merge({ message: "目前沒有等待中的協作任務" });
				return;
			}
			const draftKey = `${taskId}:${providerId}`;
			const content = (s.browserDrafts[draftKey] || "").trim();
			if (!content) {
				store.merge({ message: "請先貼上瀏覽器中的 AI 回覆" });
				return;
			}
			store.merge({ busyAction: `browser:${providerId}` });
			try {
				const result = await request("ai_nexus_collab_manual_result", {
					task_id: taskId,
					provider_id: providerId,
					content
				}, 6e4);
				if (result.ok === false) {
					throw new Error(String(result.message || "送出瀏覽器回覆失敗"));
				}
				store.merge({
					browserDrafts: { ...store.get().browserDrafts, [draftKey]: "" },
					message: String(result.message || "已匯入手動回覆")
				});
				await loadState(true);
			} catch (error) {
				store.merge({ message: error instanceof Error ? error.message : "送出瀏覽器回覆失敗" });
			} finally {
				store.merge({ busyAction: "" });
			}
		}
	};

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
