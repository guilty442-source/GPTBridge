import { getRuntimeStatusState, subscribeRuntimeStatus } from "./runtimeStatusStore.js";

/**
* Subscribe to one runtime-status field (former useRuntimeStatusField
* useSyncExternalStore wrapper).  The listener fires only when that
* field changes — modular refresh, never a whole-UI update.
* Returns an unsubscribe function.
*/
export function getRuntimeStatusField(key) {
	return getRuntimeStatusState()[key];
}

export function subscribeRuntimeStatusField(key, listener) {
	return subscribeRuntimeStatus(key, listener);
}
