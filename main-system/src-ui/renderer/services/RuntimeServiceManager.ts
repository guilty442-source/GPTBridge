import { BootLogger } from '@/shared/BootLogger'
import { eventBus } from '@/shared/RuntimeEventBus'
import { getBackendConnectionSnapshot } from '@/shared/hooks/useBackendSocket'

export type ServiceStatus =
  | 'INIT'
  | 'BOOTING'
  | 'SUCCESS'
  | 'FAIL'
  | 'TIMEOUT'
  | 'DEGRADED'
  | 'SKIP'

export interface ServiceState {
  id: string
  name: string
  status: ServiceStatus
  elapsed?: number
  error?: string
}

type ElectronApi = {
  invoke: (channel: string, ...args: unknown[]) => Promise<any>
}

type AppStatus = {
  backendManaged?: boolean
  backendMessage?: string
  backendStatus?: string
  systemReady?: boolean
  // A67 four-condition readiness (mirrored from backend /health)
  backend_runtime_ready?: boolean
  governance_ready?: boolean
  dependencies_ready?: boolean
  authenticated_ipc_connected?: boolean
}

export class RuntimeServiceManager {
  private services: Record<string, ServiceState> = {}
  private context: Record<string, unknown> = {}
  private heartbeatTimer: ReturnType<typeof setInterval> | null = null

  constructor() {
    BootLogger.log('ServiceManager', 'INIT', { mode: 'UI_FIRST' })
  }

  private async withTimeout<T>(
    promise: Promise<T>,
    ms: number,
    label: string
  ): Promise<T> {
    let timer: ReturnType<typeof setTimeout> | null = null
    try {
      return await Promise.race([
        promise,
        new Promise<never>((_, reject) => {
          timer = setTimeout(() => reject(new Error(`${label} timeout`)), ms)
        }),
      ])
    } finally {
      if (timer) clearTimeout(timer)
    }
  }

  public getAllStates(): ServiceState[] {
    return Object.values(this.services)
  }

  public async registerAndRun(
    id: string,
    name: string,
    task: (ctx: Record<string, unknown>) => Promise<unknown>,
    timeout = 3000
  ): Promise<unknown> {
    this.services[id] = { id, name, status: 'BOOTING' }
    eventBus.emit('service_update', this.getAllStates())

    const start = Date.now()
    try {
      const result = await this.withTimeout(task(this.context), timeout, name)
      const elapsed = Date.now() - start
      this.services[id] = { ...this.services[id], status: 'SUCCESS', elapsed }
      BootLogger.log(name, 'SUCCESS', { elapsed })

      if (id === 'config') {
        this.context.config = result
      }
      return result
    } catch (error) {
      const err = error as Error
      const elapsed = Date.now() - start
      const status: ServiceStatus = err.message.includes('timeout')
        ? 'TIMEOUT'
        : 'FAIL'

      this.services[id] = {
        ...this.services[id],
        status,
        error: err.message,
        elapsed,
      }
      BootLogger.log(name, status, { error: err.message })
      eventBus.emit('service_error', { id, error: err.message })
      return null
    } finally {
      eventBus.emit('service_update', this.getAllStates())
    }
  }

  public markSkipped(id: string, name: string, reason: string) {
    this.services[id] = { id, name, status: 'SKIP', error: reason }
    BootLogger.log(name, 'SKIP', { reason })
    eventBus.emit('service_update', this.getAllStates())
  }

  public startHeartbeat() {
    if (this.heartbeatTimer) return

    // Real-time: subscribe to runtime_status_push events from the backend
    // (replaces 5s polling). The backend pushes status every 2 seconds.
    this._statusPushHandler = (payload: unknown) => {
      const status = payload as AppStatus | undefined
      if (!status) return
      this._lastStatus = status
      this._updateBackendStatus(status)
      this._updateWebSocketStatus()
    }
    eventBus.on('runtime_status_push', this._statusPushHandler)

    // Also listen for the ipc_event window event (same payload, different path)
    this._ipcStatusHandler = (event: Event) => {
      const customEvent = event as CustomEvent
      const detail = customEvent.detail || {}
      if (detail.event !== 'runtime_status_push') return
      this._lastStatus = detail.payload as AppStatus
      this._updateBackendStatus(detail.payload as AppStatus)
      this._updateWebSocketStatus()
    }
    window.addEventListener('ipc_event', this._ipcStatusHandler)

    // Safety-net heartbeat: 30s fallback in case push events stop arriving.
    // A67 FORBID:stale-status — on transient IPC failure we re-fetch fresh
    // status rather than keeping a stale snapshot indefinitely.
    this.heartbeatTimer = setInterval(async () => {
      const api = (window as any).electron as ElectronApi | undefined
      if (!api?.invoke) return
      try {
        const status = (await api.invoke('app:get-status')) as AppStatus
        this._lastStatus = status
        this._updateBackendStatus(status)
        this._updateWebSocketStatus()
      } catch {
        // IPC unavailable — mark backend as degraded rather than stale.
        const backend = this.services.backend
        if (backend && backend.status !== 'SKIP' && backend.status === 'SUCCESS') {
          this.services.backend = { ...backend, status: 'DEGRADED' }
          eventBus.emit('service_update', this.getAllStates())
        }
      }
    }, 30000)
  }

  public stopHeartbeat() {
    if (this.heartbeatTimer) {
      clearInterval(this.heartbeatTimer)
      this.heartbeatTimer = null
    }
    if (this._statusPushHandler) {
      eventBus.off('runtime_status_push', this._statusPushHandler)
      this._statusPushHandler = null
    }
    if (this._ipcStatusHandler) {
      window.removeEventListener('ipc_event', this._ipcStatusHandler)
      this._ipcStatusHandler = null
    }
  }

  private _statusPushHandler: ((payload: unknown) => void) | null = null
  private _ipcStatusHandler: ((event: Event) => void) | null = null
  // Last backend status snapshot — used by _updateWebSocketStatus to apply
  // the A67 four-condition gate (socket-open alone is not ready).
  private _lastStatus: AppStatus | null = null

  private _updateBackendStatus(status: AppStatus) {
    const backend = this.services.backend
    // A67: backend ready requires all four conditions, not just systemReady.
    const nextBackendStatus: ServiceStatus = this._isFullyReady(status)
      ? 'SUCCESS'
      : 'FAIL'
    if (backend && backend.status !== 'SKIP' && backend.status !== nextBackendStatus) {
      this.services.backend = {
        ...backend,
        status: nextBackendStatus,
      }
      eventBus.emit('service_update', this.getAllStates())
    }
  }

  private _updateWebSocketStatus() {
    const websocket = this.services.websocket
    if (websocket) {
      // A67 FORBID:socket-open-alone-as-ready — the WebSocket service is only
      // SUCCESS when the socket is connected AND the backend is fully ready
      // (runtime + governance + dependencies + authenticated IPC).  While the
      // backend is still booting or degraded, the websocket status reflects
      // the degraded state rather than claiming success on socket open alone.
      const socketConnected = getBackendConnectionSnapshot().connected
      const backendReady = this._isFullyReady(this._lastStatus)
      let nextWebSocketStatus: ServiceStatus
      if (!socketConnected) {
        nextWebSocketStatus = 'FAIL'
      } else if (backendReady) {
        nextWebSocketStatus = 'SUCCESS'
      } else {
        // Socket is open but backend not fully ready — degraded, not success.
        nextWebSocketStatus = 'DEGRADED'
      }
      if (websocket.status !== nextWebSocketStatus) {
        this.services.websocket = {
          ...websocket,
          status: nextWebSocketStatus,
        }
        eventBus.emit('service_update', this.getAllStates())
      }
    }
  }

  /** A67: true only when all four readiness conditions are satisfied. */
  private _isFullyReady(status: AppStatus | undefined | null): boolean {
    if (!status) return false
    // Prefer the granular four-condition fields when the backend provides them;
    // fall back to systemReady for backward compatibility with older backends.
    if (
      'backend_runtime_ready' in status ||
      'governance_ready' in status ||
      'dependencies_ready' in status ||
      'authenticated_ipc_connected' in status
    ) {
      return Boolean(
        status.backend_runtime_ready &&
        status.governance_ready &&
        status.dependencies_ready &&
        status.authenticated_ipc_connected
      )
    }
    return Boolean(status.systemReady)
  }
}

export const serviceManager = new RuntimeServiceManager()

async function waitForBackendReady(api: any, timeoutMs = 12000) {
  const startedAt = Date.now()
  let lastStatus: any = null

  while (Date.now() - startedAt < timeoutMs) {
    lastStatus = await api.invoke('app:get-status')
    if (lastStatus?.systemReady) return lastStatus
    await new Promise((resolve) => setTimeout(resolve, 300))
  }

  const message = lastStatus?.backendMessage || lastStatus?.backendStatus || 'backend startup timeout'
  throw new Error(String(message))
}

async function waitForWebSocketReady(timeoutMs = 15000) {
  const startedAt = Date.now()
  while (Date.now() - startedAt < timeoutMs) {
    const snapshot = getBackendConnectionSnapshot()
    if (snapshot.connected) return snapshot
    await new Promise((resolve) => setTimeout(resolve, 150))
  }
  throw new Error('WebSocket connection timeout')
}

async function inspectPlatformTools(api: ElectronApi) {
  const result = await api.invoke('app:get-platform-tool-sizes')
  if (!result?.ok || !Array.isArray(result.tools)) {
    throw new Error('Platform tool inventory unavailable')
  }
  return result
}

export async function startStartupPipeline() {
  const api = (window as any).electron as ElectronApi | undefined

  const preloadReady = await serviceManager.registerAndRun(
    'preload',
    'Preload',
    async () => {
      if (!api?.invoke) throw new Error('Electron preload API unavailable')
      return true
    },
    1500
  )
  if (!preloadReady || !api) {
    eventBus.emit('boot_complete', { timestamp: Date.now(), degraded: true })
    return
  }

  const config = await serviceManager.registerAndRun(
    'config',
    'Config',
    async () => api.invoke('app:get-status'),
    3000
  ) as AppStatus | null

  const startupChecks: Array<Promise<unknown>> = [
    serviceManager.registerAndRun(
      'platform-tools',
      'Platform Tools',
      async () => inspectPlatformTools(api),
      5000
    ),
    serviceManager.registerAndRun(
      'websocket',
      'WebSocket',
      async () => waitForWebSocketReady(),
      16000
    ),
  ]

  if (config?.backendManaged === false) {
    serviceManager.markSkipped('backend', 'Backend', 'Managed externally')
  } else {
    startupChecks.push(
      serviceManager.registerAndRun(
        'backend',
        'Backend',
        async () => waitForBackendReady(api),
        13000
      )
    )
  }

  await Promise.all(startupChecks)

  serviceManager.startHeartbeat()
  eventBus.emit('boot_complete', { timestamp: Date.now() })
}
