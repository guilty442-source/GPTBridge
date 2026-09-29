/**
 * collabActions.js — AI 協作視窗動作表（B94 拆分自
 * AiCollaborationWindowApp.js 的 acts 區段）。所有動作維持原語義：
 * 經由注入的 store/request/snapshot/loadState/socket/browser 操作。
 */

export function createCollabActions({ store, snapshot, request, applyState, loadState, browser, socket }) {
	return {
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
}
