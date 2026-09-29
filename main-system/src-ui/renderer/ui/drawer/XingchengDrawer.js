import { createDrawer } from "./Drawer.js";
import { getRuntimeStatusField, subscribeRuntimeStatusField } from "@/shared/services/runtimeStatusField.js";
import { filterActiveFaults, filterActivePendingActions } from "@/shared/utils/faultPresentation";
import { mainSystemLocale } from "@/locales/main-system";
import { h, hs } from "@/shared/mini/dom.js";
const xr = mainSystemLocale.xingchengReport;
const SEVERITY_ORDER = [
	"critical",
	"high",
	"medium",
	"low",
	"info"
];
function severitySummary(faults) {
	const distribution = {};
	for (const fault of faults) {
		const severity = String(fault.severity || "info");
		distribution[severity] = (distribution[severity] || 0) + 1;
	}
	return SEVERITY_ORDER.map((severity) => [severity, Number(distribution[severity] || 0)]).filter(([, count]) => count > 0);
}
function sourceCount(faults) {
	return new Set(faults.map((fault) => String(fault.source || "").trim()).filter((source) => source.length > 0)).size;
}
function faultTime(value) {
	const raw = String(value || "");
	if (!raw) return "";
	const parsed = Date.parse(raw);
	return Number.isFinite(parsed) ? new Date(parsed).toLocaleString("zh-TW", { hour12: false }) : raw;
}
function actionExpired(action) {
	const raw = String(action.expires_at || "");
	if (!raw) return false;
	const parsed = Date.parse(raw);
	return Number.isFinite(parsed) ? Date.now() > parsed : true;
}
function actionSeverityRank(action) {
	const numeric = Number(action.risk);
	if (Number.isFinite(numeric) && numeric > 0) return numeric;
	const risk = String(action.risk || "").toLowerCase();
	if (risk.includes("critical") || risk.includes("high")) return 4;
	if (risk.includes("medium")) return 3;
	if (risk.includes("low")) return 2;
	return 1;
}
export function orderPendingActions(actions) {
	return [...actions].sort((a, b) => {
		const severity = actionSeverityRank(b) - actionSeverityRank(a);
		if (severity !== 0) return severity;
		const aTime = Date.parse(String(a.created_at || "")) || 0;
		const bTime = Date.parse(String(b.created_at || "")) || 0;
		if (aTime !== bTime) return aTime - bTime;
		return String(a.fault_id || a.action_id || "").localeCompare(String(b.fault_id || b.action_id || ""));
	});
}
const dlRow = (dt, dd) => h("div", null, h("dt", null, dt), h("dd", null, dd));

/**
* createXingchengDrawer — former AppXingchengDrawer.jsx (the star report:
* review, global faults, automation switches, pending approvals,
* management entries).
* deps: getProps() -> {review, confirmBusyId, confirmMessages, switchBusy,
*   onConfirm, onDeny, onSwitch, onNativeModelSwitch, sendCommand,
*   waitForIpcEvent, onOpenSovereign, onOpenCapacity, onOpenSlo,
*   onOpenThirdParty, onOpenSaga}, onClose
* Subscribes to the same runtime-status fields as the hook version and
* rebuilds the body while open. Returns {setOpen(open), destroy()}.
*/
export function createXingchengDrawer({ onClose, getProps }) {
	let faultBusy = false;
	let faultMessage = "";
	let modeBusy = false;
	let modeMessage = "";
	let faultDetail = null;
	let latest = {};

	const FIELDS = [
		"pending_actions",
		"automation_switches",
		"resource_mode",
		"pending_action_cardinality",
		"xingcheng_native_model_runtime",
		"global_faults"
	];
	const rebuild = () => {
		const body = drawer.body();
		if (!body) return;
		const p = getProps();
		const pendingActions = latest.pending_actions ?? [];
		const switches = latest.automation_switches ?? {};
		const resourceMode = latest.resource_mode ?? {};
		const cardinality = latest.pending_action_cardinality ?? {};
		const nativeModel = latest.xingcheng_native_model_runtime;
		const faults = latest.global_faults;
		const currentMode = String(resourceMode.mode || "medium");
		const appliedMode = String(resourceMode.applied || "");
		const autoMode = resourceMode.auto_mode === true;
		const repairSwitchOn = switches.automatic_repair_enabled === true;
		const updateSwitchOn = switches.automatic_update_enabled === true;
		const activeActions = filterActivePendingActions(pendingActions);
		const recentFaults = filterActiveFaults(faults?.recent_faults);
		const unresolvedFaults = typeof faults?.unresolved === "number" ? faults.unresolved : recentFaults.length;
		const ordered = orderPendingActions(activeActions);
		const cardinalityLabel = cardinality.mode === "MULTI_FAULT" ? xr.cardinalityMulti : cardinality.mode === "SINGLE_FAULT" ? xr.cardinalitySingle : xr.cardinalityNoFault;
		const review = p.review;
		const confirmMessages = p.confirmMessages || {};

		body.replaceChildren(h("div", { className: "xingcheng-report", dataset: { testid: "xingcheng-report-detail" } },
				h("section", { className: "xingcheng-report__summary", dataset: { tone: review.tone } },
					h("span", null, xr.currentDecision),
					h("strong", null, review.state),
					h("p", null, review.detail)),
				review.issues.length > 0
					? h("div", { className: "xingcheng-report__issues" },
						review.issues.map((issue) => h("article", { className: "xingcheng-issue" },
							h("div", { className: "xingcheng-issue__head" },
								h("strong", null, issue.title),
								h("span", null, issue.status)),
							h("dl", null,
								dlRow(xr.source, issue.source),
								dlRow(xr.details, issue.detail)))))
					: h("p", { className: "xingcheng-report__empty" }, xr.empty)),
			h("section", { className: "xingcheng-approvals", dataset: { testid: "global-faults" } },
				h("div", { className: "xingcheng-approvals__head" },
					h("strong", null, xr.globalFaultsTitle),
					h("button", {
						type: "button",
						className: "button button--ghost",
						dataset: { testid: "refresh-global-faults" },
						disabled: faultBusy,
						onClick: () => void refreshGlobalFaults(p)
					}, faultBusy ? xr.globalFaultsLoading : xr.globalFaultsRefresh)),
				h("p", { className: "xingcheng-report__empty" }, xr.globalFaultsHint),
				!faults || (faults.tracked || 0) === 0
					? h("p", { className: "xingcheng-report__empty" }, xr.globalFaultsEmpty)
					: h("div", null,
						h("div", { className: "xingcheng-switches" },
							h("div", { className: "xingcheng-switch" },
								h("span", { className: "xingcheng-switch__label" }, xr.globalFaultsTracked),
								h("strong", null, faults.tracked || 0)),
							h("div", { className: "xingcheng-switch" },
								h("span", { className: "xingcheng-switch__label" }, xr.globalFaultsUnresolved),
								h("strong", null, unresolvedFaults)),
							h("div", { className: "xingcheng-switch" },
								h("span", { className: "xingcheng-switch__label" }, xr.globalFaultsSources),
								h("strong", null, sourceCount(recentFaults)))),
						severitySummary(recentFaults).length > 0
							? h("p", { className: "xingcheng-approval__message" },
								`${xr.globalFaultsSeverity}：${severitySummary(recentFaults).map(([severity, count]) => `${severity} ${count}`).join("、")}`)
							: null,
						recentFaults.length > 0 ? h("div", { className: "xingcheng-approvals__list" },
							h("div", { className: "xingcheng-approvals__head" },
								h("strong", null, xr.globalFaultsRecent)),
							recentFaults.map((fault, index) => h("article", { className: "xingcheng-approval", key: String(fault.fault_id || `fault-${index}`) },
								h("div", { className: "xingcheng-approval__head" },
									h("span", { className: "xingcheng-approval__kind" }, fault.severity || "info"),
									h("strong", null, fault.error_class || fault.fault_type || "—")),
								h("dl", { className: "xingcheng-approval__detail" },
									dlRow(xr.source, fault.source || "—"),
									dlRow(xr.globalFaultsTarget, fault.target_entity || "—"),
									dlRow(xr.globalFaultsOutcome, fault.repair_outcome || "none"),
									dlRow(xr.globalFaultsLastSeen, faultTime(fault.timestamp))),
								h("button", {
									type: "button",
									className: "button button--ghost",
									onClick: () => {
										faultMessage = "";
										faultDetail = fault;
										rebuild();
									}
								}, xr.viewDetails)))) : null,
						(faults.top_patterns || []).length > 0 ? h("div", { className: "xingcheng-approvals__list" },
							h("div", { className: "xingcheng-approvals__head" },
								h("strong", null, xr.globalFaultsPatterns)),
							(faults.top_patterns || []).map((pattern) => h("article", { className: "xingcheng-approval" },
								h("div", { className: "xingcheng-approval__head" },
									h("span", { className: "xingcheng-approval__kind" }, pattern.severity_trend || "unknown"),
									h("strong", null, pattern.error_class || "—")),
								h("dl", { className: "xingcheng-approval__detail" },
									dlRow(xr.globalFaultsOccurrences, pattern.occurrence_count || 0),
									dlRow(xr.globalFaultsSuccessRate, typeof pattern.success_rate === "number" ? `${Math.round(pattern.success_rate * 100)}%` : "—"),
									dlRow(xr.globalFaultsAffected, (pattern.affected_entities || []).join("、") || "—"),
									dlRow(xr.globalFaultsLastSeen, faultTime(pattern.last_seen)))))) : null),
				faultDetail ? h("article", { className: "xingcheng-approval", dataset: { testid: "global-fault-detail" } },
					h("div", { className: "xingcheng-approval__head" },
						h("span", { className: "xingcheng-approval__kind" }, faultDetail.severity || "info"),
						h("strong", null, faultDetail.fault_id || faultDetail.error_class || "—")),
					h("dl", { className: "xingcheng-approval__detail" },
						dlRow(xr.globalFaultsClass, faultDetail.error_class || "—"),
						dlRow(xr.globalFaultsMessage, faultDetail.error_message || "—"),
						dlRow(xr.globalFaultsAction, faultDetail.repair_action || "—"),
						dlRow(xr.globalFaultsOutcome, faultDetail.repair_outcome || "none"),
						dlRow(xr.globalFaultsLastSeen, faultTime(faultDetail.timestamp))),
					h("button", {
						type: "button",
						className: "button button--ghost",
						onClick: () => {
							faultDetail = null;
							rebuild();
						}
					}, xr.globalFaultsClose)) : null,
				faultMessage ? h("p", { className: "xingcheng-approval__message" }, faultMessage) : null),
			h("section", { className: "xingcheng-approvals", dataset: { testid: "pending-approvals" } },
				h("div", { className: "xingcheng-approvals__head" },
					h("strong", null, xr.switchesTitle),
					h("span", null, xr.switchAttribution)),
				h("div", { className: "xingcheng-switches" },
					h("div", { className: "xingcheng-switch" },
						h("span", { className: "xingcheng-switch__label" }, xr.nativeModelSwitch),
						h("button", {
							type: "button",
							className: "xingcheng-switch__toggle",
							dataset: { tone: nativeModel?.running ? "on" : "off", testid: "switch-xingcheng-native-model" },
							disabled: p.switchBusy === "xingcheng_native_model" || nativeModel?.available === false,
							onClick: () => void p.onNativeModelSwitch(nativeModel?.running !== true)
						}, p.switchBusy === "xingcheng_native_model" ? xr.nativeModelChanging : nativeModel?.running ? xr.switchOn : xr.switchOff)),
					h("div", { className: "xingcheng-switch", dataset: { testid: "resource-mode-row" } },
						h("span", { className: "xingcheng-switch__label" },
							xr.resourceMode,
							appliedMode && appliedMode !== currentMode ? ` · ${xr.resourceModeApplying}` : ""),
						h("span", { className: "xingcheng-mode-seg" },
							[
								["sleep", xr.resourceModeSleep || "睡眠"],
								["low", xr.resourceModeLow],
								["medium", xr.resourceModeMedium],
								["high", xr.resourceModeHigh]
							].map(([mode, label]) => h("button", {
								type: "button",
								className: "xingcheng-switch__toggle",
								dataset: { tone: !autoMode && currentMode === mode ? "on" : "off", testid: `resource-mode-${mode}` },
								disabled: modeBusy,
								title: xr.resourceModeHint,
								onClick: () => void setResourceMode(mode, p)
							}, modeBusy && currentMode !== mode ? xr.resourceModeChanging : label)),
							h("button", {
								type: "button",
								className: "xingcheng-switch__toggle",
								dataset: { tone: autoMode ? "on" : "off", testid: "resource-mode-auto" },
								disabled: modeBusy,
								title: xr.resourceModeAutoHint,
								onClick: () => void setResourceMode("auto", p)
							}, xr.resourceModeAuto))),
					[
						["automatic_repair_enabled", xr.switchRepair, repairSwitchOn, true],
						["automatic_update_enabled", xr.switchUpdate, updateSwitchOn, false]
					].map(([switchName, label, enabled, managed]) => h("div", { className: "xingcheng-switch" },
						h("span", { className: "xingcheng-switch__label" },
							label,
							managed ? ` · ${xr.switchManaged}` : ""),
						h("button", {
							type: "button",
							className: "xingcheng-switch__toggle",
							dataset: { tone: enabled ? "on" : "off", testid: `switch-${switchName}` },
							disabled: managed || p.switchBusy === switchName,
							onClick: managed ? null : () => void p.onSwitch(switchName, !enabled)
						}, enabled ? xr.switchOn : xr.switchOff)))),
				confirmMessages.xingcheng_native_model ? h("p", { className: "xingcheng-approval__message" }, confirmMessages.xingcheng_native_model) : null,
				modeMessage ? h("p", { className: "xingcheng-approval__message" }, modeMessage) : null,
				h("div", { className: "xingcheng-approvals__head" },
					h("strong", null, xr.pendingTitle),
					h("span", null,
						cardinalityLabel,
						typeof cardinality.unresolved === "number" ? ` · ${xr.cardinalityCounts.replace("{count}", String(cardinality.unresolved))}` : "")),
				ordered.length === 0
					? h("p", { className: "xingcheng-report__empty" }, xr.pendingEmpty)
					: h("div", { className: "xingcheng-approvals__list" },
						ordered.map((action, index) => {
							const actionId = String(action.action_id || "");
							const busy = p.confirmBusyId === actionId;
							const message = confirmMessages[actionId];
							const pending = action.status === "awaiting-confirmation";
							const expired = pending && actionExpired(action);
							const canConfirm = pending && !expired && !busy;
							const repairPlan = action.repair_plan || {};
							const repairSteps = Array.isArray(repairPlan.steps) ? repairPlan.steps : [];
							const verification = Array.isArray(repairPlan.verification_criteria) ? repairPlan.verification_criteria.map(String).join("、") : "";
							return h("article", { className: "xingcheng-approval" },
								h("div", { className: "xingcheng-approval__head" },
									h("span", { className: "xingcheng-approval__kind" },
										action.kind === "repair" ? xr.kindRepair : xr.kindUpdate),
									h("strong", null, action.summary || actionId)),
								h("dl", { className: "xingcheng-approval__detail" },
									action.fault_id ? dlRow(xr.faultId, action.fault_id) : null,
									action.update_id ? dlRow(xr.updateId, action.update_id) : null,
									action.scope ? dlRow(xr.scopeLabel, action.scope) : null,
									action.target ? dlRow(xr.targetLabel, action.target) : null,
									action.proposed_method ? dlRow(xr.methodLabel, action.proposed_method) : null,
									repairSteps.length > 0 ? dlRow(xr.repairPlanLabel, repairSteps.map((step) => `${String(step.action || "")} → ${String(step.target || "")}`).join("；")) : null,
									verification ? dlRow(xr.verificationLabel, verification) : null,
									action.risk ? dlRow(xr.riskLabel, action.risk) : null,
									action.rollback ? dlRow(xr.rollbackLabel, action.rollback) : null,
									action.expires_at ? dlRow(xr.expiresLabel, action.expires_at) : null,
									action.evidence_digest ? dlRow(xr.evidenceLabel, action.evidence_digest.slice(0, 16)) : null),
								h("div", { className: "xingcheng-approval__meta" },
									h("span", null, expired ? xr.expired : action.status || "awaiting-confirmation"),
									h("span", null, action.created_at || "")),
								h("button", {
									type: "button",
									className: "xingcheng-approval__confirm",
									dataset: { testid: `confirm-pending-${actionId}` },
									disabled: !canConfirm,
									title: xr.singleItemPermitHint,
									onClick: () => void p.onConfirm(actionId)
								}, busy ? xr.confirming : pending ? xr.singleItemPermit : xr.confirmedDone),
								h("button", {
									type: "button",
									className: "button button--ghost",
									dataset: { testid: `deny-pending-${actionId}` },
									disabled: !pending || expired || busy,
									onClick: () => void p.onDeny(actionId)
								}, xr.doNotPermit),
								message ? h("p", { className: "xingcheng-approval__message" }, message) : null);
						}))),
			h("section", { className: "xingcheng-management", dataset: { testid: "xingcheng-management-entries" } },
				h("div", { className: "xingcheng-approvals__head" },
					h("strong", null, mainSystemLocale.app.commonEntries),
					h("span", null, mainSystemLocale.app.systemMgmt)),
				h("div", { className: "drawer-triggers", style: { display: "grid", gap: "8px", marginTop: "12px" } },
					triggerButton("S", mainSystemLocale.sovereign.title, mainSystemLocale.sovereign.eyebrow, () => p.onOpenSovereign?.()),
					triggerButton("D", mainSystemLocale.product.capacityDetails, `${mainSystemLocale.product.systemDisk} · ${mainSystemLocale.product.workspaceSize}`, () => p.onOpenCapacity?.()),
					triggerButton("P", mainSystemLocale.product.sloDashboard, mainSystemLocale.product.sloDashboardHint, () => p.onOpenSlo?.()),
					triggerButton("T", mainSystemLocale.thirdParty.title, mainSystemLocale.thirdParty.subtitle, () => p.onOpenThirdParty?.()),
					triggerButton(mainSystemLocale.sagaVisualizer.icon, mainSystemLocale.sagaVisualizer.title, mainSystemLocale.sagaVisualizer.subtitle, () => p.onOpenSaga?.()))));
	};

	const refreshGlobalFaults = async (p) => {
		faultBusy = true;
		faultMessage = "";
		rebuild();
		try {
			const sent = p.sendCommand("app:get-fault-analysis", {
				query: "overview",
				requester: "ui-xingcheng-drawer"
			});
			if (!sent.ok) {
				faultMessage = sent.message || xr.globalFaultsFailed;
				rebuild();
				return;
			}
			const result = await p.waitForIpcEvent("app:get-fault-analysis_result", 15e3);
			if (result.ok !== true) {
				faultMessage = String(result.message || "") || xr.globalFaultsFailed;
				rebuild();
				return;
			}
			p.sendCommand("app:get-runtime-status", { source: "xingcheng_fault_refresh" });
		} catch {
			faultMessage = xr.globalFaultsFailed;
		} finally {
			faultBusy = false;
		}
		rebuild();
	};

	const setResourceMode = async (mode, p) => {
		const resourceMode = latest.resource_mode ?? {};
		const currentMode = String(resourceMode.mode || "medium");
		if (modeBusy || mode === currentMode) return;
		modeBusy = true;
		modeMessage = "";
		rebuild();
		try {
			const sent = p.sendCommand("app:set-resource-mode", { mode });
			if (!sent.ok) {
				modeMessage = sent.message || xr.resourceModeFailed;
				rebuild();
				return;
			}
			const result = await p.waitForIpcEvent("app:set-resource-mode_result", 15e3);
			if (result.ok !== true) {
				modeMessage = String(result.message || "") || xr.resourceModeFailed;
				rebuild();
				return;
			}
			p.sendCommand("app:get-runtime-status", { source: "resource_mode_update" });
		} catch {
			modeMessage = xr.resourceModeFailed;
		} finally {
			modeBusy = false;
		}
		rebuild();
	};

	const drawer = createDrawer({
		onClose,
		title: xr.title,
		eyebrow: xr.eyebrow,
		icon: mainSystemLocale.app.star,
		render: rebuild
	});

	const unsubs = FIELDS.map((field) => subscribeRuntimeStatusField(field, () => {
		latest[field] = getRuntimeStatusField(field);
		if (drawer.isOpen()) rebuild();
	}));

	return {
		setOpen: (open) => {
			drawer.setOpen(open);
			if (open) {
				for (const field of FIELDS) latest[field] = getRuntimeStatusField(field);
				rebuild();
			}
		},
		destroy: () => {
			unsubs.forEach((u) => u());
			drawer.destroy();
		}
	};
}
function triggerButton(icon, title, sub, onClick) {
	return h("button", { type: "button", className: "drawer-trigger", onClick },
		h("span", { className: "drawer-trigger__icon", "aria-hidden": "true" }, icon),
		h("span", { className: "drawer-trigger__text" },
			h("strong", null, title),
			h("small", null, sub)),
		hs("svg", { className: "drawer-trigger__chevron", width: "16", height: "16", viewBox: "0 0 16 16", fill: "none" },
			hs("path", { d: "M6 3L11 8L6 13", stroke: "currentColor", "stroke-width": "1.5", "stroke-linecap": "round", "stroke-linejoin": "round" })));
}
export { createXingchengDrawer as default };
