import { hmrService } from './hmrService'

export type GlobalUpdateStrategy =
  | 'none'
  | 'renderer_hmr'
  | 'data_reload'
  | 'window_reload'
  | 'backend_restart'
  | 'app_restart'

export interface GlobalUpdateChange {
  path: string
  strategy: GlobalUpdateStrategy
  scope: string
  label: string
  reason: string
}

export interface GlobalUpdatePlan {
  changed: boolean
  changedCount: number
  highestStrategy: GlobalUpdateStrategy
  actionLabel: string
  message: string
  counts: Record<string, number>
  changes: GlobalUpdateChange[]
  generatedAt: number
}

export interface ApplyGlobalUpdateResult {
  ok: boolean
  strategy: GlobalUpdateStrategy
  message: string
  markApplied: boolean
}

const STRATEGIES = new Set<GlobalUpdateStrategy>([
  'none',
  'renderer_hmr',
  'data_reload',
  'window_reload',
  'backend_restart',
  'app_restart',
])

function toStrategy(value: unknown): GlobalUpdateStrategy {
  const strategy = String(value || 'none') as GlobalUpdateStrategy
  return STRATEGIES.has(strategy) ? strategy : 'window_reload'
}

function normalizeChange(item: unknown): GlobalUpdateChange | null {
  if (!item || typeof item !== 'object') return null
  const source = item as Record<string, unknown>
  const path = String(source.path || '').trim()
  if (!path) return null

  return {
    path,
    strategy: toStrategy(source.strategy),
    scope: String(source.scope || 'unknown'),
    label: String(source.label || '全域更新'),
    reason: String(source.reason || '偵測到專案變更。'),
  }
}

async function invokeElectron(channel: string): Promise<Record<string, unknown>> {
  const api = (window as any).electron
  if (!api?.invoke) {
    return { ok: false, message: 'Electron IPC 尚未就緒。' }
  }

  try {
    const result = await api.invoke(channel)
    return result && typeof result === 'object'
      ? (result as Record<string, unknown>)
      : { ok: true }
  } catch (error) {
    return {
      ok: false,
      message: error instanceof Error ? error.message : String(error),
    }
  }
}

export function normalizeGlobalUpdatePlan(payload: unknown): GlobalUpdatePlan {
  if (!payload || typeof payload !== 'object') {
    return {
      changed: false,
      changedCount: 0,
      highestStrategy: 'none',
      actionLabel: '無需套用',
      message: '目前沒有待套用的全域更新。',
      counts: {},
      changes: [],
      generatedAt: 0,
    }
  }

  const source = payload as Record<string, unknown>
  const changes = Array.isArray(source.changes)
    ? source.changes.map(normalizeChange).filter((item): item is GlobalUpdateChange => Boolean(item))
    : []

  const changedCount = Number(source.changed_count ?? changes.length)
  const counts =
    source.counts && typeof source.counts === 'object'
      ? Object.fromEntries(
          Object.entries(source.counts as Record<string, unknown>).map(([key, value]) => [
            key,
            Number(value) || 0,
          ])
        )
      : {}

  return {
    changed: Boolean(source.changed ?? changes.length > 0),
    changedCount: Number.isFinite(changedCount) ? Math.max(0, changedCount) : changes.length,
    highestStrategy: toStrategy(source.highest_strategy),
    actionLabel: String(source.action_label || '全域更新'),
    message: String(source.message || '更新狀態檢查完成。'),
    counts,
    changes,
    generatedAt: Number(source.generated_at ?? Date.now()) || Date.now(),
  }
}

export async function applyGlobalUpdatePlan(
  plan: GlobalUpdatePlan
): Promise<ApplyGlobalUpdateResult> {
  const strategy = plan.highestStrategy

  if (!plan.changed || strategy === 'none') {
    hmrService.reportHealthy('全域更新協調：沒有待套用變更')
    return {
      ok: true,
      strategy,
      message: '目前沒有待套用的全域更新。',
      markApplied: true,
    }
  }

  if (strategy === 'renderer_hmr') {
    hmrService.reportHealthy('全域更新協調：介面 HMR 已套用')
    return {
      ok: true,
      strategy,
      message: '介面熱更新已交由 Vite HMR 套用。',
      markApplied: true,
    }
  }

  if (strategy === 'data_reload') {
    window.dispatchEvent(
      new CustomEvent('gptbridge:global-data-reload', { detail: plan })
    )
    return {
      ok: true,
      strategy,
      message: '已通知各模組重新載入資料。',
      markApplied: true,
    }
  }

  if (strategy === 'window_reload') {
    const result = await invokeElectron('app:reload-window')
    if (result.ok === false) {
      window.location.reload()
    }
    return {
      ok: true,
      strategy,
      message: '正在重載視窗以套用變更。',
      markApplied: true,
    }
  }

  if (strategy === 'backend_restart') {
    const result = await invokeElectron('app:restart-backend')
    if (result.ok === false) {
      return {
        ok: false,
        strategy,
        message: String(
          result.message ||
            '後端需要重啟，但目前無法由 Electron 單獨重啟；請重啟 dev 流程。'
        ),
        markApplied: false,
      }
    }
    await invokeElectron('app:reload-window')
    return {
      ok: true,
      strategy,
      message: '後端已重啟，視窗正在重新連線。',
      markApplied: true,
    }
  }

  await invokeElectron('app:restart')
  return {
    ok: true,
    strategy,
    message: '正在重啟應用程式以套用全域更新。',
    markApplied: false,
  }
}
