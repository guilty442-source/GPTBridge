import { hmrService } from './hmrService'

export type GlobalUpdateStrategy =
  | 'none'
  | 'renderer_hmr'
  | 'data_reload'
  | 'window_reload'
  | 'backend_restart'
  | 'tool_restart'
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
  'tool_restart',
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
    reason: String(source.reason || '偵測到需要更新的檔案'),
  }
}

async function invokeElectron(channel: string): Promise<Record<string, unknown>> {
  const api = window.electron
  if (!api?.invoke) return { ok: false, message: 'Electron IPC 尚未就緒。' }
  try {
    const result = await api.invoke(channel)
    return result && typeof result === 'object'
      ? (result as Record<string, unknown>)
      : { ok: true }
  } catch (error) {
    return { ok: false, message: error instanceof Error ? error.message : String(error) }
  }
}

export function normalizeGlobalUpdatePlan(payload: unknown): GlobalUpdatePlan {
  if (!payload || typeof payload !== 'object') {
    return {
      changed: false,
      changedCount: 0,
      highestStrategy: 'none',
      actionLabel: '無需更新',
      message: '目前沒有待套用的更新。',
      counts: {},
      changes: [],
      generatedAt: 0,
    }
  }

  const source = payload as Record<string, unknown>
  const changes = Array.isArray(source.changes)
    ? source.changes.map(normalizeChange).filter((item): item is GlobalUpdateChange => Boolean(item))
    : []
  const rawCount = Number(source.changed_count ?? changes.length)
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
    changedCount: Number.isFinite(rawCount) ? Math.max(0, rawCount) : changes.length,
    highestStrategy: toStrategy(source.highest_strategy),
    actionLabel: String(source.action_label || '套用更新'),
    message: String(source.message || '更新狀態已就緒。'),
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
    hmrService.reportHealthy('全域更新檢查完成')
    return { ok: true, strategy, message: '目前沒有待套用的更新。', markApplied: true }
  }
  if (strategy === 'renderer_hmr') {
    hmrService.reportHealthy('前端熱更新已套用')
    return { ok: true, strategy, message: '前端變更已透過熱更新套用。', markApplied: true }
  }
  if (strategy === 'data_reload') {
    window.dispatchEvent(new CustomEvent('gptbridge:global-data-reload', { detail: plan }))
    return { ok: true, strategy, message: '資料已重新載入。', markApplied: true }
  }
  if (strategy === 'window_reload') {
    const result = await invokeElectron('app:reload-window')
    if (result.ok === false) window.location.reload()
    return { ok: true, strategy, message: '應用視窗已重新載入。', markApplied: true }
  }
  if (strategy === 'backend_restart') {
    const result = await invokeElectron('app:restart-backend')
    if (result.ok === false) {
      return {
        ok: false,
        strategy,
        message: String(result.message || '後端重新啟動失敗。'),
        markApplied: false,
      }
    }
    await invokeElectron('app:reload-window')
    return { ok: true, strategy, message: '後端已重新啟動。', markApplied: true }
  }
  if (strategy === 'tool_restart') {
    window.dispatchEvent(new CustomEvent('gptbridge:global-data-reload', { detail: plan }))
    return { ok: true, strategy, message: '受影響的獨立工具已更新。', markApplied: true }
  }

  await invokeElectron('app:restart')
  return { ok: true, strategy, message: '主程式正在重新啟動以完成更新。', markApplied: false }
}
