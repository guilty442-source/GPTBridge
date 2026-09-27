let state = {};
const listeners = new Map();
export function getRuntimeStatusState() {
	return state;
}
export function subscribeRuntimeStatus(key, listener) {
	let bucket = listeners.get(key);
	if (!bucket) {
		bucket = new Set();
		listeners.set(key, bucket);
	}
	bucket.add(listener);
	return () => {
		bucket?.delete(listener);
	};
}
function isEqual(a, b) {
	// Reports replace values wholesale; reference/primitive comparison is
	// O(1) and never blocks the UI (no deep serialization on hot paths).
	return Object.is(a, b);
}
/**
* Apply a partial report.  Fields that did not change are ignored, and
* listeners are notified only for fields that changed —this is what keeps
* the refresh modular instead of a whole-UI update.
*/
export function applyRuntimeStatusReport(report) {
	if (!report || typeof report !== "object") return;
	const changed = [];
	let next = null;
	for (const key of Object.keys(report)) {
		const value = report[key];
		if (value === undefined) continue;
		if (isEqual(state[key], value)) continue;
		next ??= { ...state };
		// @ts-expect-error indexed assignment across the payload union
		next[key] = value;
		changed.push(key);
	}
	if (changed.length === 0 || next === null) return;
	state = next;
	for (const key of changed) {
		const bucket = listeners.get(key);
		if (!bucket) continue;
		for (const listener of Array.from(bucket)) {
			try {
				listener();
			} catch {}
		}
	}
}
export function resetRuntimeStatusStore() {
	state = {};
}
