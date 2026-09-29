import { createBasePanel } from "./BasePanel.js";
import { mainSystemLocale } from "@/locales/main-system";
import { h } from "@/shared/mini/dom.js";
const ec = mainSystemLocale.externalCollaboration;
const t = mainSystemLocale.toolbox;
const COLLABORATION_ACTION_TIMEOUT_MS = 12e4;

/**
* createExternalCollaborationPanel — former ExternalCollaborationPanel.jsx.
* deps: {onClose, sendCommand, waitForIpcEvent, getConnected}
* Returns {setOpen(open), destroy()}.
*/
export function createExternalCollaborationPanel({ onClose, sendCommand, waitForIpcEvent, getConnected }) {
	const panel = createBasePanel({
		onClose,
		title: ec.title,
		eyebrow: ec.eyebrow,
		icon: ec.icon,
		getConnected,
		content: (ctx) => mountExternalCollaborationContent(ctx)
	});
	panel.bind(sendCommand, waitForIpcEvent);
	return panel;
}

function mountExternalCollaborationContent({ locale, send, waitForEvent, generateRequestId, clearError, state }) {
	let status = "stopped";
	let busy = false;
	let errorMessage = "";

	const statusBadge = h("span", { className: "base-panel-status-badge stopped" }, ec.statusStopped);
	const errorSpan = h("span", { className: "base-panel-error", style: { display: "none" } });
	const startBtn = h("button", {
		type: "button",
		className: "base-panel-btn base-panel-btn--primary",
		disabled: !state.connected,
		onClick: () => void handleStart()
	}, ec.launch);
	const stopBtn = h("button", {
		type: "button",
		className: "base-panel-btn base-panel-btn--secondary",
		disabled: true,
		onClick: () => void handleStop()
	}, locale.stop);
	const disconnectedEl = h("div", { className: "base-panel__disconnected", style: { display: "none" } }, locale.disconnected);

	const handleStart = async () => {
		if (busy || !state.connected) return;
		busy = true;
		clearError();
		status = "running";
		update();
		const requestId = generateRequestId("ai-collaboration:start");
		try {
			const waitResult = waitForEvent("toolbox_start_tool_result", COLLABORATION_ACTION_TIMEOUT_MS, (payload) => String(payload.request_id || "") === requestId);
			const sent = send("toolbox_start_tool", {
				tool_id: "ai-collaboration",
				request_id: requestId
			});
			if (!sent.ok) throw new Error(sent.message || locale.launch);
			const result = await waitResult;
			if (result.ok === false) throw new Error(String(result.message || locale.launch));
			status = "running";
		} catch (error) {
			const message = error instanceof Error ? error.message : locale.launch;
			status = "error";
			errorMessage = message;
		} finally {
			busy = false;
		}
		update();
	};

	const handleStop = async () => {
		if (busy || !state.connected) return;
		busy = true;
		clearError();
		update();
		const requestId = generateRequestId("ai-collaboration:stop");
		try {
			const waitResult = waitForEvent("toolbox_force_close_tool_result", COLLABORATION_ACTION_TIMEOUT_MS, (payload) => String(payload.request_id || "") === requestId);
			const sent = send("toolbox_force_close_tool", {
				tool_id: "ai-collaboration",
				request_id: requestId
			});
			if (!sent.ok) throw new Error(sent.message || locale.stop);
			const result = await waitResult;
			if (result.ok === false) throw new Error(String(result.message || locale.stop));
			status = "stopped";
		} catch (error) {
			const message = error instanceof Error ? error.message : locale.stop;
			status = "error";
			errorMessage = message;
		} finally {
			busy = false;
		}
		update();
	};

	function update() {
		statusBadge.className = `base-panel-status-badge ${status}`;
		statusBadge.textContent = status === "running" ? ec.statusRunning : status === "error" ? ec.statusError : ec.statusStopped;
		errorSpan.style.display = errorMessage ? "" : "none";
		errorSpan.textContent = errorMessage;
		startBtn.disabled = busy || !state.connected;
		startBtn.className = `base-panel-btn base-panel-btn--primary ${busy ? "base-panel-btn--loading" : ""}`;
		startBtn.textContent = status === "running" ? ec.statusRunning : busy ? ec.launching : ec.launch;
		stopBtn.disabled = busy || !state.connected || status !== "running";
		stopBtn.textContent = busy ? locale.closing : locale.stop;
		disconnectedEl.style.display = state.connected ? "none" : "";
	}

	const el = h("div", null,
		h("section", { className: "base-panel-section" },
			h("h4", { className: "base-panel-section__title-text" }, ec.description)),
		h("section", { className: "base-panel-section" },
			h("h4", { className: "base-panel-section__title-text" }, ec.aiList),
			h("ul", { className: "base-panel-feature-list" },
				h("li", null, h("strong", null, ec.ai01), " — ", ec.coordinator, ec.coordinatorFixed),
				h("li", null, h("strong", null, ec.ai02), " — ", ec.searchProvider, ec.searchProviderAdvanced),
				h("li", null, h("strong", null, ec.ai03), " — ", ec.calculationAdvancedSearch),
				h("li", null, h("strong", null, ec.ai04), " — ", ec.longText),
				h("li", null, h("strong", null, ec.ai05), " — ", ec.reasoning),
				h("li", null, h("strong", null, ec.ai06), " — ", ec.socialMediaTrendsNews)),
			h("p", { className: "base-panel-detail" }, ec.detailMaxParallel)),
		h("section", { className: "base-panel-section" },
			h("h4", { className: "base-panel-section__title-text" }, ec.collaborativeDiagnosis),
			h("ul", { className: "base-panel-feature-list" },
				h("li", null, ec.groupQuery),
				h("li", null, ec.sharedMemory),
				h("li", null, ec.reportExport))),
		h("section", { className: "base-panel-section" },
			h("div", { className: "base-panel-section__header" },
				h("h4", { className: "base-panel-section__title-text" }, ec.status)),
			h("div", { className: "base-panel-status-row" }, statusBadge, errorSpan)),
		h("section", { className: "base-panel-section base-panel-actions" }, startBtn, stopBtn),
		disconnectedEl);

	update();
	return { el, update, destroy: () => {} };
}
export { createExternalCollaborationPanel as default };
