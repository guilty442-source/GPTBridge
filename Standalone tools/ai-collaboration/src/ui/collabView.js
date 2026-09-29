//! collabView.js — ai-collaboration window DOM builders (React-free,
//! E180/C116).  Pure view functions: each takes a state snapshot plus the
//! app's action callbacks and returns DOM subtrees; the mount code owns
//! the state store and re-renders these sections on change.
import { h } from "../../../../shared-layer/src/ui/toolWindow/dom.js";

export const COLLAB_MODES = [
	{ id: "single", label: "單一 AI" },
	{ id: "compare", label: "多 AI 比較" },
	{ id: "sequential_review", label: "依序審查" }
];
export const PROMPT_PRESETS = [
	{ id: "summarize", label: "摘要", prompt: "請整理重點、共識、分歧與需要補資料的地方，最後列出 3 個下一步。" },
	{ id: "compare", label: "比較", prompt: "請用表格比較各方案的優點、缺點、風險、成本、適用情境，最後給出建議排序。" },
	{ id: "challenge", label: "反證", prompt: "請刻意找出這個想法可能錯在哪裡，列出反例、盲點、失敗條件與驗證方式。" },
	{ id: "plan", label: "行動", prompt: "請把這件事拆成可執行步驟，標出優先順序、依賴、風險與完成定義。" }
];

export function responseLabel(status) {
	if (status === "completed") return "完成";
	if (status === "running") return "執行中";
	if (status === "waiting_verification") return "等待驗證";
	if (status === "awaiting-user") return "等待瀏覽器操作";
	if (status === "waiting") return "等待前序結果";
	if (status === "failed") return "失敗";
	if (status === "cancelled") return "已取消";
	if (status === "opened") return "已開啟";
	if (status === "waiting_user") return "等待使用者";
	if (status === "aggregating") return "彙整中";
	if (status === "partial") return "部分完成";
	return "待命";
}
export function responseText(response) {
	const value = response.content ?? response.answer ?? response.response ?? response.text ?? "";
	return String(value);
}
export function socketStatusLabel(status) {
	if (status === "Connected") return "已連線";
	if (status === "Connecting") return "連線中";
	if (status === "Disconnected") return "未連線";
	if (status === "Error") return "連線錯誤";
	return status;
}

/// Status -> dot class suffix shared by agent rail and response cards.
function statusKind(status) {
	if (status === "completed" || status === "opened") return "ok";
	if (status === "running" || status === "aggregating") return "run";
	if (status === "failed") return "fail";
	if (status === "cancelled") return "idle";
	if (status === "awaiting-user" || status === "waiting" ||
		status === "waiting_user" || status === "waiting_verification" ||
		status === "partial") return "wait";
	return "idle";
}
function statusDot(status) {
	return h("span", {
		className: `ai-collab-dot ai-collab-dot--${statusKind(status)}`,
		title: responseLabel(status)
	});
}
/// Provider monogram — colour-keyed by data-provider for fast scanning.
function providerMark(agent) {
	const provider = String(agent.provider || agent.provider_id || "ai").toLowerCase();
	const initial = (String(agent.name || provider).trim()[0] || "A").toUpperCase();
	return h("span", {
		className: "ai-collab-mark",
		dataset: { provider }
	}, initial);
}

/// Collaboration-mode segmented control (replaces the plain select).
export function buildModeSegments(currentMode, onPick) {
	return h("div", { className: "ai-collab-segment", role: "radiogroup", "aria-label": "協作模式" },
		COLLAB_MODES.map((mode) => h("button", {
			type: "button",
			role: "radio",
			"aria-checked": String(mode.id === currentMode),
			className: `ai-collab-segment-btn${mode.id === currentMode ? " is-active" : ""}`,
			onClick: () => onPick(mode.id)
		}, mode.label)));
}

/// Agent selection rail — one card per provider; the card body toggles
/// selection, compact icon actions open/login per agent.
export function buildAgentRail(s, acts) {
	const busy = Boolean(s.busyAction);
	if (s.agents.length === 0) {
		return h("div", { className: "ai-collab-rail-empty" },
			h("span", { className: "ai-collab-muted" }, "AI 名單載入中，請確認後端連線..."));
	}
	const cards = s.agents.map((agent) => {
		const selected = s.selectedAgents.has(agent.agent_id);
		return h("article", {
			className: `ai-collab-agentcard${selected ? " is-selected" : ""}`,
			dataset: { status: statusKind(agent.status) }
		},
			h("label", { className: "ai-collab-agentcard-main" },
				h("input", {
					type: "checkbox",
					checked: selected,
					disabled: busy,
					onChange: () => void acts.toggleAgent(agent.agent_id)
				}),
				providerMark(agent),
				h("span", { className: "ai-collab-agentcard-meta" },
					h("strong", null, agent.name),
					h("small", null,
						statusDot(agent.status),
						responseLabel(agent.status)))),
			h("span", { className: "ai-collab-agentcard-actions" },
				h("button", {
					type: "button", className: "ai-collab-mini",
					disabled: busy,
					onClick: () => void acts.openAgent(agent.agent_id)
				}, s.busyAction === `open:${agent.agent_id}` ? "…" : "開啟"),
				h("button", {
					type: "button", className: "ai-collab-mini",
					disabled: busy,
					onClick: () => void acts.authorizeAgent(agent.agent_id)
				}, s.busyAction === `authorize:${agent.agent_id}` ? "…" : "登入")));
	});
	return h("div", { className: "ai-collab-rail" },
		h("div", { className: "ai-collab-rail-cards", role: "group", "aria-label": "內建 AI 名單" }, cards),
		h("div", { className: "ai-collab-rail-side" },
			h("span", { className: "ai-collab-count", "aria-label": "已選取數量" },
				h("strong", null, String(s.selectedAgents.size)),
				`/ ${s.agents.length}`),
			h("button", {
				type: "button", className: "ai-collab-primary",
				disabled: busy || s.selectedAgents.size === 0,
				onClick: () => void acts.openSelectedAgents()
			}, s.busyAction === "open-selected" ? "開啟中…" : "開啟選取")));
}

/// Settings drawer panel — mounted only while ``settingsOpen`` is true.
export function buildSettings(s, acts) {
	const busy = Boolean(s.busyAction);
	const blockInput = (value, placeholder, field) => h("input", {
		type: "text", value, placeholder, disabled: busy,
		dataset: { k: `new-agent-${field}` },
		onInput: (event) => acts.setField(field, event.target.value)
	});
	return h("div", { className: "ai-collab-settings", role: "group", "aria-label": "設定" },
		h("div", { className: "ai-collab-settings-head" },
			h("strong", null, "設定"),
			h("button", {
				type: "button", className: "ai-collab-mini",
				onClick: () => acts.setSettingsOpen(false)
			}, "關閉")),
		h("div", { className: "ai-collab-settings-grid" },
			h("div", { className: "ai-collab-settings-block" },
				h("span", null, "新增 AI 名單"),
				blockInput(s.newAgentName, "AI 名稱，例如 Copilot", "newAgentName"),
				blockInput(s.newAgentProvider, "提供者（可留空）", "newAgentProvider"),
				h("input", {
					type: "url", value: s.newAgentUrl, placeholder: "https://...", disabled: busy,
					dataset: { k: "new-agent-url" },
					onInput: (event) => acts.setField("newAgentUrl", event.target.value)
				}),
				h("button", {
					type: "button", className: "ai-collab-primary",
					disabled: busy || !s.newAgentName.trim() || !s.newAgentUrl.trim(),
					onClick: () => void acts.addAgent()
				}, s.busyAction === "add-agent" ? "新增中…" : "新增 AI")),
			h("div", { className: "ai-collab-settings-block" },
				h("span", null, "各 AI 網址設定"),
				h("div", { className: "ai-collab-settings-agents" },
					s.agents.map((agent) => h("details", { className: "ai-collab-agent-settings" },
						h("summary", null,
							providerMark(agent),
							agent.name),
						h("label", null,
							h("span", null, "一般業務 URL"),
							h("input", {
								type: "url", value: agent.general_url || "", disabled: busy,
								dataset: { k: `agent-url-${agent.agent_id}` },
								onInput: (event) => acts.updateAgentSetting(agent.agent_id, "general_url", event.target.value)
							})),
						h("div", { className: "ai-collab-agent-business-flags" },
							h("label", null,
								h("input", {
									type: "checkbox",
									checked: Boolean(agent.general_enabled),
									onChange: (event) => acts.updateAgentSetting(agent.agent_id, "general_enabled", event.target.checked ? 1 : 0)
								}),
								"一般"),
							h("button", {
								type: "button", className: "ai-collab-mini", disabled: busy,
								onClick: () => void acts.saveAgentBusinessSettings(agent)
							}, s.busyAction === `settings:${agent.agent_id}` ? "儲存中…" : "儲存 URL")))))));
}

/// Collaboration task selector inside the responses head.
export function buildTaskSelect(s, acts) {
	if (!s.collabTasks.length) return null;
	return h("select", {
		className: "ai-collab-task-select",
		"aria-label": "選擇協作任務",
		onChange: (event) => acts.setActiveTaskId(event.target.value)
	}, s.collabTasks.map((task) => h("option", {
		value: String(task.task_id),
		selected: String(task.task_id) === s.activeTaskId
	}, `${responseLabel(String(task.overall_status || ""))} · ${String(task.mode || "")} · ${String(task.original_request || "").slice(0, 20)}`)));
}

/// One provider result card inside the active task — left accent edge
/// keyed by status kind; awaiting-user gets the manual-import block.
function buildProviderResult(result, s, acts) {
	const providerId = String(result.provider_id || "");
	const status = String(result.response_status || "");
	const draftKey = `${s.activeTask ? s.activeTask.task_id : ""}:${providerId}`;
	const text = String(result.response_text || "");
	const busy = Boolean(s.busyAction);
	return h("article", {
		className: "ai-collab-response",
		dataset: { status: statusKind(status), provider: providerId.toLowerCase() }
	},
		h("div", { className: "ai-collab-response-head" },
			h("span", { className: "ai-collab-response-title" },
				statusDot(status), h("strong", null, providerId)),
			h("span", { className: "ai-collab-response-badges" },
				result.capture_method ? h("span", { className: "ai-collab-badge" },
					result.capture_method === "MANUAL" ? "手動匯入" : "自動擷取") : null)),
		text ? h("p", { className: "ai-collab-response-body" }, text) : null,
		status === "awaiting-user" ? h("div", { className: "ai-collab-browser-submit" },
			h("textarea", {
				value: s.browserDrafts[draftKey] || "",
				placeholder: "完成瀏覽器操作後，將 AI 回覆貼回這裡再送出。",
				disabled: busy,
				dataset: { k: `draft-${draftKey}` },
				onInput: (event) => acts.setBrowserDraft(draftKey, event.target.value)
			}),
			h("button", {
				type: "button", className: "ai-collab-primary",
				disabled: busy || !(s.browserDrafts[draftKey] || "").trim(),
				onClick: () => void acts.submitBrowserResult(providerId)
			}, s.busyAction === `browser:${providerId}` ? "送出中…" : "手動匯入")) : null,
		result.error_code ? h("p", { className: "ai-collab-error-inline" }, String(result.error_code)) : null);
}

/// Comparison aggregate card for a completed multi-agent task.
function buildComparison(comparison) {
	const has = (comparison.common_points?.length || comparison.differences?.length);
	if (!has) return null;
	const group = (label, items, render) => items.length > 0
		? h("div", { className: "ai-collab-compare-group" },
			h("span", { className: "ai-collab-compare-label" }, label),
			items.map((item, index) => h("p", { key: `i${index}`, className: "ai-collab-response-body" }, render(item))))
		: null;
	return h("article", { className: "ai-collab-response ai-collab-comparison" },
		h("div", { className: "ai-collab-response-head" },
			h("span", { className: "ai-collab-response-title" }, h("strong", null, "比較結果"))),
		h("div", { className: "ai-collab-compare-grid" },
			group("共同觀點", comparison.common_points || [], (item) => `· ${item.text}`),
			group("各 AI 差異", comparison.differences || [], (item) => `· [${item.source_provider}] ${item.text}`),
			group("相互矛盾", comparison.contradictions || [], (item) => `· ${(item.statements || []).map((st) => `[${st.provider_id}] ${st.text}`).join(" / ")}`),
			group("尚未回答", comparison.unanswered_questions || [], (item) => `· ${item.question}`))));
}

/// Synthesis card for a completed task.
function buildSynthesis(synthesis) {
	if (!synthesis?.summary) return null;
	return h("article", { className: "ai-collab-response ai-collab-synthesis" },
		h("div", { className: "ai-collab-response-head" },
			h("span", { className: "ai-collab-response-title" }, h("strong", null, "整合結果")),
			h("span", { className: "ai-collab-badge ai-collab-badge--accent" },
				synthesis.method === "governed-model" ? "受管模型" : "規則彙整")),
		h("p", { className: "ai-collab-response-body ai-collab-synthesis-body" }, synthesis.summary));
}

/// Legacy single-request response card (messageId-tracked results).
function buildLegacyResponse(response, s, acts) {
	const agentId = String(response.agent_id || "");
	const status = String(response.status || "");
	const text = responseText(response);
	return h("article", {
		className: "ai-collab-response",
		dataset: { status: statusKind(status), provider: agentId.toLowerCase() }
	},
		h("div", { className: "ai-collab-response-head" },
			h("span", { className: "ai-collab-response-title" },
				statusDot(status), h("strong", null, s.agentsById.get(agentId)?.name || agentId || "AI"))),
		text ? h("p", { className: "ai-collab-response-body" }, text) : null,
		response.error ? h("p", { className: "ai-collab-error-inline" }, String(response.error)) : null);
}

/// The full responses list — rebuilt on task/response/draft changes.
export function buildResponsesBody(s, acts) {
	if (s.activeTask) {
		return [
			(s.activeTask.provider_results || []).map((result) => buildProviderResult(result, s, acts)),
			buildComparison(s.activeTask.comparison || {}),
			buildSynthesis(s.activeTask.synthesis)
		];
	}
	if (!s.responses.length) {
		return h("div", { className: "ai-collab-empty" },
			h("span", { className: "ai-collab-empty-mark" }, "◇"),
			h("p", { className: "ai-collab-muted" }, "送出協作後，AI 回應會顯示在這裡。"));
	}
	return s.responses.map((response) => buildLegacyResponse(response, s, acts));
}
