import { createBasePanel } from "./BasePanel.js";
import { mainSystemLocale } from "@/locales/main-system";
import { h, hs, renderList } from "@/shared/mini/dom.js";
const tp = mainSystemLocale.thirdParty;

/**
* createThirdPartyPanel — former ThirdPartyPanel.jsx.
* deps: {onClose, sendCommand, waitForIpcEvent, getConnected}
* Returns {setOpen(open), destroy()}.
*/
export function createThirdPartyPanel({ onClose, sendCommand, waitForIpcEvent, getConnected }) {
	const panel = createBasePanel({
		onClose,
		title: tp.title,
		eyebrow: tp.eyebrow,
		icon: tp.icon,
		getConnected,
		content: (ctx) => mountThirdPartyContent(ctx)
	});
	panel.bind(sendCommand, waitForIpcEvent);
	return panel;
}

function mountThirdPartyContent({ state, locale, send, waitForEvent }) {
	let status = null;
	let loadingState = "idle";
	let errorMsg = "";
	let updatingTool = null;
	const updateResults = {};

	const header = h("section", { className: "base-panel-section" },
		h("div", { className: "base-panel-section__header" },
			h("h4", { className: "base-panel-section__title-text" }, tp.title),
			h("p", null, tp.subtitle)),
		h("div", { className: "base-panel-actions" },
			h("button", { className: "base-panel-btn base-panel-btn--secondary", onClick: () => void probeVersions() }, tp.refreshVersions),
			h("button", { className: "base-panel-btn base-panel-btn--secondary", onClick: () => void checkUpdates() }, tp.checkUpdates),
			h("button", { className: "base-panel-btn base-panel-btn--primary", onClick: () => void refreshStatus() }, tp.refresh)));
	const [probeBtn, checkBtn, refreshBtn] = header.querySelectorAll("button");

	const errorEl = h("div", { className: "base-panel__error", role: "alert", style: { display: "none" } },
		h("span", null),
		h("button", {
			type: "button",
			className: "base-panel__error-dismiss",
			"aria-label": mainSystemLocale.common.close,
			onClick: () => {
				errorMsg = "";
				update();
			}
		},
			hs("svg", { width: "14", height: "14", viewBox: "0 0 14 14", fill: "none" },
				hs("path", { d: "M4 4L10 10M10 4L4 10", stroke: "currentColor", "stroke-width": "1.5", "stroke-linecap": "round" }))));

	const metaSection = h("section", { className: "base-panel-section" });
	const tbody = h("tbody");
	const table = h("div", { className: "base-panel-table-wrap" },
		h("table", { className: "base-panel-table" },
			h("thead", null,
				h("tr", null,
					h("th", null, tp.tool),
					h("th", null, "記錄版本"),
					h("th", null, tp.detectedVersion),
					h("th", null, tp.status),
					h("th", null, tp.latestVersion),
					h("th", null, tp.updatable),
					h("th", null, tp.action))),
			tbody));

	const el = h("div", null, header, errorEl, metaSection, table);

	const refreshStatus = async () => {
		loadingState = "loading";
		update();
		const result = send("app:get-third-party-status", {});
		if (!result.ok && !result.queued) {
			loadingState = "error";
			errorMsg = result.message || tp.errorLoadStatus;
			update();
			return;
		}
		try {
			const payload = await waitForEvent("app:get-third-party-status_result", 1e4);
			if (payload.ok) {
				status = payload;
				loadingState = "success";
			} else {
				loadingState = "error";
				errorMsg = tp.errorLoadStatus;
			}
		} catch {
			loadingState = "error";
			errorMsg = tp.timeout;
		}
		update();
	};

	const probeVersions = async () => {
		loadingState = "loading";
		update();
		const result = send("app:probe-third-party-versions", {});
		if (!result.ok && !result.queued) {
			loadingState = "error";
			errorMsg = result.message || tp.errorProbeFailed;
			update();
			return;
		}
		try {
			const payload = await waitForEvent("app:probe-third-party-versions_result", 6e4);
			if (payload.ok) {
				status = {
					ok: true,
					status: {
						...status?.status || {
							version: "",
							inventory_path: "",
							auto_updatable_tools: [],
							last_full_probe_at: null,
							last_full_update_check_at: null,
							versions: {},
							update_checks: {}
						},
						versions: payload.versions,
						last_full_probe_at: new Date().toISOString()
					}
				};
				loadingState = "success";
			} else {
				loadingState = "error";
				errorMsg = tp.statusLabels.error;
			}
		} catch {
			loadingState = "error";
			errorMsg = tp.timeout;
		}
		update();
	};

	const checkUpdates = async () => {
		loadingState = "loading";
		update();
		const result = send("app:check-third-party-updates", {});
		if (!result.ok && !result.queued) {
			loadingState = "error";
			errorMsg = result.message || tp.errorCheckFailed;
			update();
			return;
		}
		try {
			const payload = await waitForEvent("app:check-third-party-updates_result", 12e4);
			if (payload.ok) {
				status = {
					ok: true,
					status: {
						...status?.status || {
							version: "",
							inventory_path: "",
							auto_updatable_tools: [],
							last_full_probe_at: null,
							last_full_update_check_at: null,
							versions: {},
							update_checks: {}
						},
						update_checks: payload.updates,
						last_full_update_check_at: new Date().toISOString()
					}
				};
				loadingState = "success";
			} else {
				loadingState = "error";
				errorMsg = tp.errorCheckFailed;
			}
		} catch {
			loadingState = "error";
			errorMsg = tp.timeout;
		}
		update();
	};

	const updateTool = async (toolId) => {
		updatingTool = toolId;
		update();
		try {
			const result = send("app:update-third-party-tool", {
				tool_id: toolId,
				approval_token: "governance-auto-approve"
			});
			if (!result.ok && !result.queued) {
				errorMsg = result.message || tp.errorUpdateFailed;
				update();
				return;
			}
			const payload = await waitForEvent("app:update-third-party-tool_result", 12e4);
			updateResults[toolId] = payload;
			void probeVersions();
		} catch {
			errorMsg = tp.timeout;
		} finally {
			updatingTool = null;
		}
		update();
	};

	function update() {
		const busy = loadingState === "loading" || state.loading;
		probeBtn.disabled = busy;
		checkBtn.disabled = busy;
		refreshBtn.disabled = busy;
		if (errorMsg) {
			errorEl.style.display = "";
			errorEl.firstChild.textContent = errorMsg;
		} else {
			errorEl.style.display = "none";
		}
		metaSection.replaceChildren();
		if (status?.status) {
			const autoUpdatable = status.status.auto_updatable_tools || [];
			metaSection.appendChild(h("div", { className: "base-panel-meta" },
				h("div", { className: "base-panel-meta__item" },
					h("span", { className: "base-panel-meta__label" }, tp.serviceVersion),
					h("strong", { className: "base-panel-meta__value" }, status.status.version)),
				h("div", { className: "base-panel-meta__item" },
					h("span", { className: "base-panel-meta__label" }, tp.autoUpdatable),
					h("strong", { className: "base-panel-meta__value" }, autoUpdatable.join(", ") || "—")),
				status.status.last_full_probe_at ? h("div", { className: "base-panel-meta__item" },
					h("span", { className: "base-panel-meta__label" }, tp.lastProbe),
					h("strong", { className: "base-panel-meta__value base-panel-meta__time" },
						new Date(status.status.last_full_probe_at).toLocaleString("zh-TW", { hour12: false }))) : null));
		}
		const versions = status?.status?.versions || {};
		const updateChecks = status?.status?.update_checks || {};
		const autoUpdatable = status?.status?.auto_updatable_tools || [];
		const toolIds = Object.keys(versions).sort();
		if (toolIds.length === 0) {
			tbody.replaceChildren(h("tr", null,
				h("td", { colSpan: 7, className: "base-panel-empty" },
					loadingState === "loading" ? tp.loading : tp.empty)));
			return;
		}
		renderList(tbody, toolIds, (toolId) => {
			const info = versions[toolId];
			const update = updateChecks[toolId];
			const canUpdate = autoUpdatable.includes(toolId);
			const result = updateResults[toolId];
			const isUpdating = updatingTool === toolId;
			const statusKey = info?.status || "";
			const statusLabels = tp.statusLabels;
			const tone = statusLabels[statusKey] ? "success" : "muted";
			return h("tr", null,
				h("td", { className: "base-panel-table__tool-id" }, toolId),
				h("td", { className: "base-panel-table__version" }, info?.recorded_version || "—"),
				h("td", { className: "base-panel-table__version" }, info?.detected_version || "—"),
				h("td", null,
					h("span", { className: "base-panel-status tp-status--" + tone },
						statusLabels[statusKey] || info?.status || "—")),
				h("td", { className: "base-panel-table__version" }, update?.latest_version || "—"),
				h("td", null,
					canUpdate
						? h("span", { className: "base-panel-badge base-panel-badge--yes" }, tp.autoUpdatableBadge)
						: h("span", { className: "base-panel-badge base-panel-badge--no" }, tp.manualBadge)),
				h("td", null,
					canUpdate ? h("button", {
						className: "base-panel-btn base-panel-btn--small base-panel-btn--update",
						disabled: isUpdating,
						onClick: () => void updateTool(toolId)
					}, isUpdating ? tp.updating : tp.updateBtn) : null,
					result ? h("span", { className: "base-panel-update-result " + (result.ok ? "base-panel-update-result--ok" : "base-panel-update-result--fail") },
						result.ok ? "✓ " + (result.after_version || tp.updated) : "✗ " + (result.error || tp.errorUpdateFailed)) : null));
		});
	}

	void refreshStatus();
	return { el, update, destroy: () => {} };
}
export { createThirdPartyPanel as default };
