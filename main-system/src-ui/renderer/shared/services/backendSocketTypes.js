export const INITIAL_STATE = {
	status: "Disconnected",
	lastStatusAt: null,
	lastError: "",
	reconnectAttempt: 0,
	queuedCommands: 0
};
export const WS_RECONNECT_BASE_DELAY_MS = 500;
export const WS_RECONNECT_MAX_DELAY_MS = 16e3;
export const WS_CONNECT_TIMEOUT_MS = 8e3;
export const WS_READINESS_RETRY_MS = 1500;
export const WS_COMMAND_QUEUE_MAX = 50;
export const WS_COMMAND_QUEUE_TTL_MS = 6e4;
// Stale-connection threshold: the backend sends heartbeat_ping every 5s
// and closes silent sockets at 20s; we tolerate ~2 missed pings before
// treating the connection as dead.
export const WS_STALE_CONNECTION_MS = 25e3;
export const OUTBOX_CURSOR_KEY = "gptbridge.outbox.cursor";
export const OUTBOX_GENERATION_KEY = "gptbridge.outbox.generation";
export const OUTBOX_BUFFER_MAX = 500;
const openBackendSockets = new Set();
let backendConnectionSnapshot = {
	status: INITIAL_STATE.status,
	connected: false,
	updatedAt: null
};
export function updateBackendConnectionSnapshot(status, socket, connected) {
	if (socket && connected === true) {
		openBackendSockets.add(socket);
	} else if (socket && connected === false) {
		openBackendSockets.delete(socket);
	}
	const hasOpenSocket = openBackendSockets.size > 0;
	backendConnectionSnapshot = {
		status: hasOpenSocket ? "Connected" : status,
		connected: hasOpenSocket,
		updatedAt: Date.now()
	};
	return backendConnectionSnapshot;
}
export function getBackendConnectionSnapshot() {
	return { ...backendConnectionSnapshot };
}
