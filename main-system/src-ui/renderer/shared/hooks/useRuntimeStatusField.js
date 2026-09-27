import { useSyncExternalStore } from "react";
import { getRuntimeStatusState, subscribeRuntimeStatus } from "../services/runtimeStatusStore.js";
/**
* Subscribe to one runtime-status field.  The component re-renders only
* when that field changes —modular refresh, never a whole-UI update.
*/
export function useRuntimeStatusField(key) {
	return useSyncExternalStore((listener) => subscribeRuntimeStatus(key, listener), () => getRuntimeStatusState()[key]);
}
