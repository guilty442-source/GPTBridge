import { BootLogger } from '@/shared/BootLogger'
import { eventBus } from '@/shared/RuntimeEventBus'

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
    return Promise.race([
      promise,
      new Promise<never>((_, reject) =>
        setTimeout(() => reject(new Error(`${label} timeout`)), ms)
      ),
    ])
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
    this.services[id] = { id, name, status: 'INIT' }
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

  public startHeartbeat() {
    if (this.heartbeatTimer) return

    this.heartbeatTimer = setInterval(async () => {
      const api = (window as any).electron
      if (!api?.invoke) return

      try {
        const status = await api.invoke('app:get-status')
        const backend = this.services.backend
        if (!backend) return

        const nextStatus: ServiceStatus = status?.systemReady ? 'SUCCESS' : 'FAIL'
        if (backend.status !== nextStatus) {
          this.services.backend = {
            ...backend,
            status: nextStatus,
          }
          eventBus.emit('service_update', this.getAllStates())
        }
      } catch {
        // Keep last known state during transient IPC failures.
      }
    }, 5000)
  }
}

export const serviceManager = new RuntimeServiceManager()

async function waitForBackendReady(api: any, timeoutMs = 12000) {
  const startedAt = Date.now()
  let lastStatus: any = null

  while (Date.now() - startedAt < timeoutMs) {
    lastStatus = await api?.invoke('app:get-status')
    if (lastStatus?.systemReady) return lastStatus
    await new Promise((resolve) => setTimeout(resolve, 300))
  }

  const message = lastStatus?.backendMessage || lastStatus?.backendStatus || 'backend startup timeout'
  throw new Error(String(message))
}

export async function startStartupPipeline() {
  const api = (window as any).electron

  await serviceManager.registerAndRun(
    'preload',
    'Preload',
    async () => Boolean(api),
    1500
  )

  const config = await serviceManager.registerAndRun(
    'config',
    'Config',
    async () => api?.invoke('app:get-status'),
    3000
  )

  await Promise.all([
    serviceManager.registerAndRun(
      'database',
      'Database',
      async () => new Promise((resolve) => setTimeout(resolve, 100))
    ),
    serviceManager.registerAndRun(
      'websocket',
      'WebSocket',
      async () => new Promise((resolve) => setTimeout(resolve, 160))
    ),
    serviceManager.registerAndRun(
      'plugins',
      'Plugins',
      async () => new Promise((resolve) => setTimeout(resolve, 220))
    ),
  ])

  if ((config as any)?.backendStatus === 'manual_dev_mode') {
    BootLogger.log('Backend', 'SKIP', { reason: 'Dev Mode' })
  } else {
    await serviceManager.registerAndRun(
      'backend',
      'Backend',
      async () => waitForBackendReady(api),
      13000
    )
  }

  serviceManager.startHeartbeat()
  eventBus.emit('boot_complete', { timestamp: Date.now() })
}
