/**
* backendRecovery — governed, guarded request path for backend restart.
*
* E48 boundaries: the frontend may only REQUEST a backend restart; the
* Electron main process owns the boot_core restart execution.  This service
* enforces a cooldown and an attempt cap so a single broken startup cannot
* hammer the orchestrator (FORBID:duplicate owner / ungoverned restart).
*/
const RECOVERY_COOLDOWN_MS = 2e4;
const MAX_RECOVERY_ATTEMPTS = 3;
let lastRequestAt = 0;
let attempts = 0;
function gptBridgeApi() {
	return window.gptBridge;
}
export function resetBackendRecovery() {
	attempts = 0;
}
export function getBackendRecoveryState() {
	return {
		attempts,
		lastRequestAt
	};
}
export async function requestBackendRestart(reason) {
	const api = gptBridgeApi();
	if (!api || typeof api.restartBackend !== "function") {
		return {
			requested: false,
			reason: "restart-backend-unavailable"
		};
	}
	const now = Date.now();
	if (now - lastRequestAt < RECOVERY_COOLDOWN_MS) {
		return {
			requested: false,
			reason: "cooldown"
		};
	}
	if (attempts >= MAX_RECOVERY_ATTEMPTS) {
		return {
			requested: false,
			reason: "attempts-exhausted"
		};
	}
	attempts += 1;
	lastRequestAt = now;
	try {
		await api.restartBackend();
		return {
			requested: true,
			reason
		};
	} catch (error) {
		const message = error instanceof Error ? error.message : String(error);
		return {
			requested: false,
			reason: message
		};
	}
}
