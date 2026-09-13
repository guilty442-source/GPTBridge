/**
 * Backend socket constants, types, and connection snapshot.
 *
 * Shared state and helpers used by the useBackendSocket hook.
 */

export type BackendSocketState = {
  status: string
  lastStatusAt: number | null
  lastError: string
  reconnectAttempt: number
  queuedCommands: number
}

export type SendCommandResult = {
  ok: boolean
  queued: boolean
  message?: string
}

export type BackendConnectionSnapshot = {
  status: string
  connected: boolean
  updatedAt: number | null
}

export const INITIAL_STATE: BackendSocketState = {
  status: 'Disconnected',
  lastStatusAt: null,
  lastError: '',
  reconnectAttempt: 0,
  queuedCommands: 0,
}

export const WS_RECONNECT_BASE_DELAY_MS = 500
export const WS_RECONNECT_MAX_DELAY_MS = 16000
export const WS_CONNECT_TIMEOUT_MS = 8000
export const WS_READINESS_RETRY_MS = 1500
export const WS_COMMAND_QUEUE_MAX = 50
export const WS_COMMAND_QUEUE_TTL_MS = 60_000
// Stale-connection threshold: the backend sends heartbeat_ping every 5s
// and closes silent sockets at 20s; we tolerate ~2 missed pings before
// treating the connection as dead.
export const WS_STALE_CONNECTION_MS = 25_000
export const OUTBOX_CURSOR_KEY = 'gptbridge.outbox.cursor'
export const OUTBOX_GENERATION_KEY = 'gptbridge.outbox.generation'
export const OUTBOX_BUFFER_MAX = 500

export type OutboxStateEvent = {
  sequence: number
  entity_id: string
  entity_type: string
  operation: string
  authoritative_revision: number
  previous_revision: number
  changed_field_allowlist: string[]
  invalidation_keys: string[]
  state_hash: string
  backend_generation: string
  release_id: string
  contract_version: string
  correlation_id: string
  committed_at: string
  idempotency_key?: string
  session_id?: string
}

const openBackendSockets = new Set<WebSocket>()

let backendConnectionSnapshot: BackendConnectionSnapshot = {
  status: INITIAL_STATE.status,
  connected: false,
  updatedAt: null,
}

export function updateBackendConnectionSnapshot(
  status: string,
  socket?: WebSocket,
  connected?: boolean
): BackendConnectionSnapshot {
  if (socket && connected === true) {
    openBackendSockets.add(socket)
  } else if (socket && connected === false) {
    openBackendSockets.delete(socket)
  }

  const hasOpenSocket = openBackendSockets.size > 0
  backendConnectionSnapshot = {
    status: hasOpenSocket ? 'Connected' : status,
    connected: hasOpenSocket,
    updatedAt: Date.now(),
  }

  return backendConnectionSnapshot
}

export function getBackendConnectionSnapshot(): BackendConnectionSnapshot {
  return { ...backendConnectionSnapshot }
}
