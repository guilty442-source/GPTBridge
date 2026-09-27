const ACTIVE_FAULT_OUTCOMES = new Set([
	"pending",
	"failure",
	"quarantined"
]);
const ACTIVE_ACTION_STATUSES = new Set([
	"awaiting-confirmation",
	"confirmed",
	"executing",
	"pending"
]);
const TERMINAL_EVIDENCE_STATUSES = new Set([
	"expired",
	"invalidated",
	"executed",
	"failed",
	"completed",
	"cancelled",
	"canceled",
	"rolled-back",
	"rolled_back",
	"reverted",
	"revoked",
	"removed",
	"reconciled",
	"superseded",
	"resolved",
	"obsolete",
	"skipped",
	"success",
	"succeeded",
	"done",
	"closed",
	"rejected"
]);
export function normalizeStatus(value) {
	return String(value ?? "").trim().toLowerCase();
}
export function isPastExpiry(expiresAt, now = Date.now()) {
	const raw = String(expiresAt ?? "").trim();
	if (!raw) return false;
	const parsed = Date.parse(raw);
	return Number.isFinite(parsed) ? now > parsed : false;
}
export function isActiveFault(fault) {
	if (!fault || typeof fault !== "object") return false;
	const outcome = normalizeStatus(fault.repair_outcome);
	if (!ACTIVE_FAULT_OUTCOMES.has(outcome)) return false;
	const evidence = fault.raw_evidence;
	if (evidence && typeof evidence === "object" && !Array.isArray(evidence)) {
		const evidenceStatus = normalizeStatus(evidence.status);
		if (evidenceStatus && TERMINAL_EVIDENCE_STATUSES.has(evidenceStatus)) {
			return false;
		}
	}
	return true;
}
export function filterActiveFaults(faults) {
	if (!Array.isArray(faults)) return [];
	return faults.filter(isActiveFault);
}
export function isActivePendingAction(action) {
	if (!action || typeof action !== "object") return false;
	const status = normalizeStatus(action.status);
	if (status && !ACTIVE_ACTION_STATUSES.has(status)) return false;
	return !isPastExpiry(action.expires_at);
}
export function filterActivePendingActions(actions) {
	if (!Array.isArray(actions)) return [];
	return actions.filter(isActivePendingAction);
}
