const STATE_KEY = "gptbridge_hmr_inspector_state";
const MAX_LEVEL = 5;
const ACTION_COOLDOWN_MS = 2500;
const STABLE_RESET_MS = 1e4;
const DISCONNECT_GRACE_MS = 3e3;
const RECOVERY_TIMEOUT_MS = 12e3;
const defaultSnapshot = {
	level: 0,
	mode: "healthy",
	lastReason: "",
	lastEventAt: null,
	lastSuccessAt: null,
	failureCount: 0,
	disconnectCount: 0,
	recoveryPendingSince: null
};
const listeners = new Set();
let snapshot = readPersistedSnapshot();
let initialized = false;
let stableResetTimer = null;
let watchdogTimer = null;
let disconnectTimer = null;
let lastActionAt = 0;
let fullReloadMarks = [];
function readPersistedSnapshot() {
	try {
		const raw = sessionStorage.getItem(STATE_KEY);
		if (!raw) return { ...defaultSnapshot };
		const parsed = JSON.parse(raw);
		return {
			level: typeof parsed.level === "number" && Number.isFinite(parsed.level) ? Math.max(0, Math.min(MAX_LEVEL, Math.trunc(parsed.level))) : 0,
			mode: parsed.mode === "healthy" || parsed.mode === "warning" || parsed.mode === "recovering" || parsed.mode === "force-restart" ? parsed.mode : "healthy",
			lastReason: typeof parsed.lastReason === "string" ? parsed.lastReason : "",
			lastEventAt: typeof parsed.lastEventAt === "number" && Number.isFinite(parsed.lastEventAt) ? parsed.lastEventAt : null,
			lastSuccessAt: typeof parsed.lastSuccessAt === "number" && Number.isFinite(parsed.lastSuccessAt) ? parsed.lastSuccessAt : null,
			failureCount: typeof parsed.failureCount === "number" && Number.isFinite(parsed.failureCount) ? Math.max(0, Math.trunc(parsed.failureCount)) : 0,
			disconnectCount: typeof parsed.disconnectCount === "number" && Number.isFinite(parsed.disconnectCount) ? Math.max(0, Math.trunc(parsed.disconnectCount)) : 0,
			recoveryPendingSince: typeof parsed.recoveryPendingSince === "number" && Number.isFinite(parsed.recoveryPendingSince) ? parsed.recoveryPendingSince : null
		};
	} catch {
		return { ...defaultSnapshot };
	}
}
function persistSnapshot() {
	sessionStorage.setItem(STATE_KEY, JSON.stringify(snapshot));
}
function emit() {
	persistSnapshot();
	const current = { ...snapshot };
	for (const listener of listeners) {
		listener(current);
	}
}
function updateSnapshot(patch) {
	snapshot = {
		...snapshot,
		...patch
	};
	emit();
}
function scheduleStableReset() {
	if (stableResetTimer) clearTimeout(stableResetTimer);
	stableResetTimer = setTimeout(() => {
		snapshot = {
			...snapshot,
			level: 0,
			mode: "healthy",
			lastReason: "",
			recoveryPendingSince: null
		};
		emit();
	}, STABLE_RESET_MS);
}
function markHmrHealthy(reason) {
	const now = Date.now();
	updateSnapshot({
		mode: "healthy",
		lastReason: reason,
		lastEventAt: now,
		lastSuccessAt: now,
		recoveryPendingSince: null
	});
	if (snapshot.level > 0) {
		scheduleStableReset();
	}
}
function isMajorFailure(reason) {
	const text = reason.toLowerCase();
	return text.includes("beforefullreload") || text.includes("failed to fetch dynamically imported module") || text.includes("loading chunk") || text.includes("cannot apply hmr") || text.includes("full reload");
}
async function invokeElectron(channel) {
	const api = window.electron;
	if (!api?.invoke) return false;
	try {
		await api.invoke(channel);
		return true;
	} catch (error) {
		console.error(`[HMR Inspector] ipc ${channel} failed:`, error);
		return false;
	}
}
async function performRecovery(level, reason) {
	const now = Date.now();
	if (now - lastActionAt < ACTION_COOLDOWN_MS) return;
	lastActionAt = now;
	if (level <= 1) return;
	if (level === 2) {
		window.location.reload();
		return;
	}
	if (level === 3) {
		const ok = await invokeElectron("app:reload-window");
		if (!ok) window.location.reload();
		return;
	}
	if (level === 4) {
		const ok = await invokeElectron("app:reload-window-hard");
		if (!ok) window.location.reload();
		return;
	}
	const restarted = await invokeElectron("app:restart");
	if (!restarted) {
		console.error("[HMR Inspector] app restart failed, fallback to page reload");
		window.location.reload();
	}
	console.error(`[HMR Inspector] forced app restart due to: ${reason}`);
}
async function escalate(reason, options = {}) {
	const now = Date.now();
	let nextLevel = Math.max(snapshot.level + 1, options.minLevel ?? 1);
	if (isMajorFailure(reason)) {
		nextLevel = Math.max(nextLevel, 4);
	}
	if (options.forceRestart) {
		nextLevel = MAX_LEVEL;
	}
	nextLevel = Math.max(1, Math.min(MAX_LEVEL, nextLevel));
	updateSnapshot({
		level: nextLevel,
		mode: nextLevel >= MAX_LEVEL ? "force-restart" : "recovering",
		lastReason: reason,
		lastEventAt: now,
		failureCount: snapshot.failureCount + 1,
		recoveryPendingSince: now
	});
	console.warn(`[HMR Inspector] escalation level=${nextLevel}, reason=${reason}`);
	await performRecovery(nextLevel, reason);
}
function setupWindowErrorHooks() {
	window.addEventListener("error", (event) => {
		const message = String(event.message ?? "");
		if (!message) return;
		if (/failed to fetch dynamically imported module/i.test(message) || /loading chunk [\d]+ failed/i.test(message) || /cannot apply hmr update/i.test(message)) {
			void escalate(`window:error:${message}`, { minLevel: 4 });
		}
	});
	window.addEventListener("unhandledrejection", (event) => {
		const reason = event.reason;
		const text = reason instanceof Error ? reason.message : typeof reason === "string" ? reason : JSON.stringify(reason);
		if (/failed to fetch dynamically imported module/i.test(text) || /loading chunk [\d]+ failed/i.test(text)) {
			void escalate(`window:unhandledrejection:${text}`, { minLevel: 4 });
		}
	});
}
function setupHotHooks() {
	const hot = import.meta.hot;
	if (!hot) return;
	hot.on("vite:afterUpdate", () => {
		markHmrHealthy("HMR update applied");
	});
	hot.on("vite:error", (data) => {
		const message = data?.err?.message ?? "unknown vite error";
		void escalate(`vite:error:${message}`, { minLevel: 2 });
	});
	hot.on("vite:ws:disconnect", () => {
		updateSnapshot({
			mode: "warning",
			lastReason: "HMR websocket disconnected",
			lastEventAt: Date.now(),
			disconnectCount: snapshot.disconnectCount + 1,
			recoveryPendingSince: Date.now()
		});
		if (disconnectTimer) clearTimeout(disconnectTimer);
		disconnectTimer = setTimeout(() => {
			if (snapshot.recoveryPendingSince) {
				void escalate("hmr websocket disconnect timeout", { minLevel: 3 });
			}
		}, DISCONNECT_GRACE_MS);
	});
	hot.on("vite:beforeFullReload", (payload) => {
		const now = Date.now();
		const path = typeof payload?.path === "string" ? payload.path : "unknown";
		fullReloadMarks = [...fullReloadMarks, now].filter((time) => now - time < 2e4);
		if (fullReloadMarks.length >= 2) {
			void escalate(`beforeFullReload repeated on ${path}`, { minLevel: 4 });
			return;
		}
		updateSnapshot({
			mode: "warning",
			lastReason: `major change detected (${path})`,
			lastEventAt: now,
			recoveryPendingSince: now
		});
	});
}
function setupBackendHotReloadHook() {
	window.addEventListener("ipc_event", (event) => {
		const detail = event.detail;
		if (!["runtime:hot-reload-completed", "maintenance:hot-reload-completed"].includes(String(detail?.event || "")) || detail.payload?.ok !== true) {
			return;
		}
		markHmrHealthy("Backend hot-reload applied; refreshing renderer");
		window.setTimeout(() => {
			window.location.reload();
		}, 100);
	});
}
function setupWatchdog() {
	if (watchdogTimer) clearInterval(watchdogTimer);
	// Perf/low-CPU: recovery timeout is 12s; 5s sampling halves wakeups with
	// at most +2.5s escalation delay. Early-returns when idle either way.
	// idle-ok: recovery-pending is already the idle gate; running recovery
	// while hidden leaves the user a healthy session on return.
	watchdogTimer = setInterval(() => {
		if (!snapshot.recoveryPendingSince) return;
		if (Date.now() - snapshot.recoveryPendingSince > RECOVERY_TIMEOUT_MS) {
			void escalate("hmr recovery timeout", { minLevel: Math.min(MAX_LEVEL, snapshot.level + 1) });
		}
	}, 5e3);
}
export const hmrService = {
	init: () => {
		if (initialized) return;
		initialized = true;
		setupHotHooks();
		setupBackendHotReloadHook();
		setupWindowErrorHooks();
		setupWatchdog();
		if (snapshot.level > 0) {
			scheduleStableReset();
		}
	},
	getLevel: () => snapshot.level,
	getSnapshot: () => ({ ...snapshot }),
	subscribe: (listener) => {
		listeners.add(listener);
		listener({ ...snapshot });
		return () => {
			listeners.delete(listener);
		};
	},
	clearLevel: () => {
		snapshot = {
			...defaultSnapshot,
			lastSuccessAt: Date.now()
		};
		emit();
	},
	forceRestart: async (reason = "manual force restart") => {
		await escalate(reason, { forceRestart: true });
	},
	reportHealthy: (reason = "manual healthy signal") => {
		markHmrHealthy(reason);
	}
};
