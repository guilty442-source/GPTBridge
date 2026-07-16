import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  formatRunOutput,
  parseToolJson,
  useToolRunner,
} from './toolWindowRunner'
import './project-cleaner.css'

type CleanupScope = 'global' | 'runtime' | 'sandbox'
type CleanupMode =
  | 'idle'
  | 'preview'
  | 'quarantine'
  | 'delete'
  | 'purge'
  | 'restore'
  | 'pin'
  | 'analyze'
  | 'rescue'
  | 'rescue-repair'
  | 'auto'
  | 'preferences'

interface LockingProcess {
  pid?: number
  name?: string
  restartable?: boolean
}

interface CleanupItem {
  item_id?: string
  path?: string
  type?: string
  size_bytes?: number
  reason?: string
  rule_id?: string
  risk?: string
  age_days?: number
  min_age_days?: number
}

interface CleanupNotice {
  path?: string
  reason?: string
  message?: string
  type?: string
  locked_by?: LockingProcess[]
}

interface CleanupSummaryBucket {
  count?: number
  size_bytes?: number
}

interface CleanupSummary {
  by_risk?: Record<string, CleanupSummaryBucket>
  by_reason?: Record<string, CleanupSummaryBucket>
  by_type?: Record<string, CleanupSummaryBucket>
  largest_items?: CleanupItem[]
}

interface CleanupHealth {
  state?: string
  safety_level?: string
  score?: number
  safety_score?: number
  cleanliness_score?: number
  confidence_score?: number
  item_count?: number
  low_count?: number
  medium_count?: number
  high_count?: number
  error_count?: number
  skipped_count?: number
  total_bytes?: number
  recommended_action?: string
  recommendation?: string
  requires_review?: boolean
  direct_delete_allowed?: boolean
  quarantine_ttl_hours?: number
  batch_count?: number
  expired_count?: number
  incomplete_count?: number
  pinned_count?: number
  ttl_hours?: number
  scope?: string
}

interface CleanerPayload {
  ok?: boolean
  operation?: string
  state?: string
  authority?: string
  boundary?: string
  repairable_count?: number
  repaired_count?: number
  unresolved_count?: number
  blocking?: string[]
  scope?: string
  dry_run?: boolean
  quarantine?: boolean
  automatic?: boolean
  skipped?: CleanupNotice[] | boolean
  cleaned_files?: number
  cleaned_dirs?: number
  cleaned_bytes?: number
  planned_files?: number
  planned_dirs?: number
  planned_bytes?: number
  restored?: number
  renamed?: number
  purged_dirs?: number
  purged_bytes?: number
  quarantine_path?: string
  quarantine_manifest?: string
  plan_id?: string
  plan_token?: string
  plan_expires_in_minutes?: number
  summary?: CleanupSummary
  health?: CleanupHealth
  items?: CleanupItem[]
  errors?: CleanupNotice[]
  skipped_count?: number
  error_count?: number
  message?: string
  duplicate_groups?: DuplicateGroup[]
  duplicate_group_count?: number
  duplicate_wasted_bytes?: number
  large_stale_files?: LargeFile[]
  large_stale_count?: number
  file_count?: number
  total_bytes?: number
}

interface QuarantineBatch {
  name?: string
  path?: string
  created_at?: string
  scope?: string
  status?: string
  item_count?: number
  restorable_count?: number
  size_bytes?: number
  age_hours?: number
  expires_at?: string
  expired?: boolean
  pinned?: boolean
  recoverable?: boolean
  manifest_path?: string
}

interface HistoryRecord {
  operation_id?: string
  timestamp?: string
  action?: string
  ok?: boolean
  scope?: string
  item_count?: number
  bytes?: number
  errors?: number
  batch_id?: string
}

interface CleanerStatus {
  ok?: boolean
  version?: string
  project_root?: string
  quarantine?: {
    batches?: QuarantineBatch[]
    health?: CleanupHealth
  }
  quarantine_health?: CleanupHealth
  history?: HistoryRecord[]
  rules?: {
    schema_version?: number
    override_path?: string
    override_exists?: boolean
    directory_rule_count?: number
    file_rule_count?: number
    warnings?: string[]
  }
  automation?: {
    enabled?: boolean
    scope?: string
    interval_hours?: number
    disk_free_threshold_percent?: number
    max_bytes_per_run?: number
  }
  disk?: {
    total_bytes?: number
    used_bytes?: number
    free_bytes?: number
    free_percent?: number
  }
  message?: string
}

interface DuplicateGroup {
  sha256?: string
  size_bytes?: number
  copies?: number
  wasted_bytes?: number
  paths?: string[]
}

interface LargeFile {
  path?: string
  size_bytes?: number
  age_days?: number
}

interface ProgressEvent {
  tool_id?: string
  phase?: string
  percent?: number
  message?: string
  current_path?: string
  completed?: number
  total?: number
}

const SCOPE_OPTIONS: Array<{ id: CleanupScope; label: string; detail: string }> = [
  { id: 'global', label: '整個專案', detail: '專案快取、暫存與過期輸出' },
  { id: 'runtime', label: 'Runtime', detail: '執行期暫存，保留 Profile 與狀態' },
  { id: 'sandbox', label: 'Sandbox', detail: 'RuntimeSandbox 工作檔與快取' },
]

function scopeLabel(scope: string | undefined): string {
  return SCOPE_OPTIONS.find((item) => item.id === scope)?.label || '整個專案'
}

function formatBytes(value: number | undefined): string {
  const bytes = Number(value ?? 0)
  if (!Number.isFinite(bytes) || bytes <= 0) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let size = bytes
  let unit = 0
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024
    unit += 1
  }
  return `${size >= 10 || unit === 0 ? size.toFixed(0) : size.toFixed(1)} ${units[unit]}`
}

function formatDateTime(value: string | undefined): string {
  if (!value) return '-'
  const timestamp = Date.parse(value)
  return Number.isFinite(timestamp)
    ? new Date(timestamp).toLocaleString('zh-TW', { hour12: false })
    : value
}

function riskLabel(risk: string | undefined): string {
  if (risk === 'high') return '高'
  if (risk === 'medium') return '中'
  return '低'
}

function healthTitle(health: CleanupHealth | undefined): string {
  if (!health) return '等待掃描'
  if (health.state === 'clean') return '乾淨'
  if (health.state === 'ready') return '可執行'
  if (health.state === 'review') return '需檢查'
  if (health.state === 'attention') return '需處理'
  if (health.state === 'healthy') return '健康'
  if (health.state === 'empty') return '空白'
  return health.state || '等待掃描'
}

function resultMessage(payload: CleanerPayload): string {
  if (payload.ok === false) return payload.message || '操作失敗。'
  if (payload.operation === 'system-rescue-check') {
    return payload.state === 'healthy'
      ? '系統救援檢查完成：專案、設定與封裝均正常。'
      : `系統救援檢查完成：發現 ${payload.repairable_count || 0} 個可修復異常。`
  }
  if (payload.operation === 'system-rescue-repair') {
    return `系統救援修復完成：已修復 ${payload.repaired_count || 0} 個，尚餘 ${payload.unresolved_count || 0} 個。`
  }
  if (typeof payload.restored === 'number') {
    return `已還原 ${payload.restored} 個項目${payload.renamed ? `，重新命名 ${payload.renamed} 個` : ''}。`
  }
  if (typeof payload.purged_dirs === 'number') {
    return `已清除 ${payload.purged_dirs} 個隔離批次，釋放 ${formatBytes(payload.purged_bytes)}。`
  }
  if (typeof payload.duplicate_group_count === 'number') {
    return `分析完成：${payload.duplicate_group_count} 組重複檔案，可回收 ${formatBytes(payload.duplicate_wasted_bytes)}。`
  }
  if (payload.dry_run) {
    return `預覽完成：${(payload.planned_files || 0) + (payload.planned_dirs || 0)} 個候選，共 ${formatBytes(payload.planned_bytes)}。`
  }
  if (payload.automatic && payload.skipped === true) return payload.message || '智慧整理目前不需執行。'
  return `${payload.quarantine ? '已隔離' : '已清理'} ${(payload.cleaned_files || 0) + (payload.cleaned_dirs || 0)} 個項目，共 ${formatBytes(payload.cleaned_bytes)}。`
}

function historyText(record: HistoryRecord): string {
  const actionLabels: Record<string, string> = {
    preview: '預覽',
    quarantine: '隔離',
    delete: '刪除',
    restore: '還原',
    purge: '清除隔離區',
    analyze: '空間分析',
    'auto-clean': '智慧整理',
    pin: '釘選',
    unpin: '取消釘選',
    preferences: '設定',
  }
  const detail = [
    typeof record.item_count === 'number' ? `${record.item_count} 項` : '',
    typeof record.bytes === 'number' ? formatBytes(record.bytes) : '',
    record.scope ? scopeLabel(record.scope) : '',
  ].filter(Boolean)
  return `${formatDateTime(record.timestamp)} | ${actionLabels[record.action || ''] || record.action || '操作'}${detail.length ? ` | ${detail.join(' | ')}` : ''}`
}

export function ProjectCleanerWindowApp() {
  const { cancelToolRun, requestToolRun, socketStatus } = useToolRunner(
    'project-cleaner',
    15 * 60 * 1000
  )
  const [scope, setScope] = useState<CleanupScope>('runtime')
  const [mode, setMode] = useState<CleanupMode>('idle')
  const [message, setMessage] = useState('先建立預覽計畫，再選取要處理的項目。')
  const [result, setResult] = useState<CleanerPayload | null>(null)
  const [analysis, setAnalysis] = useState<CleanerPayload | null>(null)
  const [status, setStatus] = useState<CleanerStatus | null>(null)
  const [selectedItemIds, setSelectedItemIds] = useState<Set<string>>(new Set())
  const [selectedBatch, setSelectedBatch] = useState('')
  const [conflictStrategy, setConflictStrategy] = useState<'skip' | 'rename'>('skip')
  const [riskFilter, setRiskFilter] = useState('all')
  const [typeFilter, setTypeFilter] = useState('all')
  const [sortMode, setSortMode] = useState('size-desc')
  const [progress, setProgress] = useState<ProgressEvent | null>(null)
  const [automationEnabled, setAutomationEnabled] = useState(false)
  const [ttlHours, setTtlHours] = useState(24)
  const [sessionHistory, setSessionHistory] = useState<string[]>([])

  const busy = mode !== 'idle'
  const batches = status?.quarantine?.batches || []

  const appendSessionHistory = useCallback((text: string) => {
    const clock = new Date().toLocaleTimeString('zh-TW', { hour12: false })
    setSessionHistory((current) => [`${clock} | ${text}`, ...current].slice(0, 8))
  }, [])

  const refreshStatus = useCallback(async () => {
    try {
      const run = await requestToolRun(['--status', '--json'])
      const payload = parseToolJson<CleanerStatus>(run.stdout)
      if (!payload) return
      setStatus(payload)
      setAutomationEnabled(Boolean(payload.automation?.enabled))
      setTtlHours(Number(payload.quarantine_health?.ttl_hours || 24))
      const nextBatches = payload.quarantine?.batches || []
      setSelectedBatch((current) =>
        current && nextBatches.some((batch) => batch.name === current)
          ? current
          : nextBatches[0]?.name || ''
      )
    } catch {
      setStatus(null)
    }
  }, [requestToolRun])

  useEffect(() => {
    void refreshStatus()
  }, [refreshStatus])

  useEffect(() => {
    const handler = (event: Event) => {
      const detail = (event as CustomEvent).detail || {}
      if (detail.event !== 'toolbox_run_tool_progress') return
      const payload = (detail.payload || {}) as ProgressEvent
      if (payload.tool_id !== 'project-cleaner') return
      setProgress(payload)
    }
    window.addEventListener('ipc_event', handler)
    return () => window.removeEventListener('ipc_event', handler)
  }, [])

  const execute = useCallback(
    async (
      args: string[],
      nextMode: CleanupMode,
      startText: string,
      options: { replaceResult?: boolean; refresh?: boolean } = {}
    ): Promise<CleanerPayload | null> => {
      setMode(nextMode)
      setProgress({ phase: 'prepare', percent: 0, message: startText })
      setMessage(startText)
      try {
        const run = await requestToolRun([...args, '--progress-jsonl'])
        const payload =
          parseToolJson<CleanerPayload>(run.stdout) || ({
            ok: run.ok,
            message: formatRunOutput(run) || run.message,
          } as CleanerPayload)
        if (options.replaceResult !== false) setResult(payload)
        const nextMessage = resultMessage(payload)
        setMessage(nextMessage)
        appendSessionHistory(nextMessage)
        if (options.refresh) await refreshStatus()
        return payload
      } catch (error) {
        const nextMessage = error instanceof Error ? error.message : '清理工具執行失敗。'
        const payload = { ok: false, scope, message: nextMessage }
        if (options.replaceResult !== false) setResult(payload)
        setMessage(nextMessage)
        appendSessionHistory(nextMessage)
        return payload
      } finally {
        setMode('idle')
      }
    },
    [appendSessionHistory, refreshStatus, requestToolRun, scope]
  )

  const previewCleanup = useCallback(async () => {
    const payload = await execute(
      ['--cleanup-garbage', '--scope', scope, '--dry-run', '--json'],
      'preview',
      `${scopeLabel(scope)}掃描中。`
    )
    const lowRiskIds = (payload?.items || [])
      .filter((item) => item.risk === 'low' && item.item_id)
      .map((item) => String(item.item_id))
    setSelectedItemIds(new Set(lowRiskIds))
  }, [execute, scope])

  const planArgs = useMemo(() => {
    if (!result?.dry_run || !result.plan_id || !result.plan_token) return []
    const args = ['--plan-id', result.plan_id, '--plan-token', result.plan_token]
    for (const itemId of selectedItemIds) args.push('--selected-item', itemId)
    return args
  }, [result, selectedItemIds])

  const quarantineCleanup = useCallback(async () => {
    const payload = await execute(
      ['--cleanup-garbage', '--scope', scope, '--quarantine', ...planArgs, '--json'],
      'quarantine',
      `${scopeLabel(scope)}選取項目隔離中。`,
      { refresh: true }
    )
    if (payload?.ok) setSelectedItemIds(new Set())
  }, [execute, planArgs, scope])

  const deleteCleanup = useCallback(async () => {
    if (!window.confirm(`永久刪除 ${selectedItemIds.size} 個低風險項目？此操作無法還原。`)) return
    const payload = await execute(
      [
        '--cleanup-garbage',
        '--scope',
        scope,
        ...planArgs,
        '--confirm-direct-delete',
        '--json',
      ],
      'delete',
      `${scopeLabel(scope)}永久清理中。`,
      { refresh: true }
    )
    if (payload?.ok) setSelectedItemIds(new Set())
  }, [execute, planArgs, scope, selectedItemIds.size])

  const analyzeStorage = useCallback(async () => {
    const payload = await execute(
      ['--analyze-storage', '--scope', scope, '--json'],
      'analyze',
      `${scopeLabel(scope)}儲存空間分析中。`,
      { replaceResult: false, refresh: true }
    )
    if (payload) setAnalysis(payload)
  }, [execute, scope])

  const systemRescueCheck = useCallback(async () => {
    await execute(
      ['--system-rescue-check', '--json'],
      'rescue',
      '系統救援檢查中。',
      { refresh: true }
    )
  }, [execute])

  const systemRescueRepair = useCallback(async () => {
    if (!window.confirm('要由專案清理工具執行可復原的系統救援修復嗎？')) return
    await execute(
      ['--system-rescue-repair', '--json'],
      'rescue-repair',
      '系統救援安全修復中。',
      { refresh: true }
    )
  }, [execute])

  const automaticCleanup = useCallback(async () => {
    if (!window.confirm('執行一次智慧整理？只會把低風險項目移入隔離區。')) return
    await execute(
      ['--auto-clean', '--force', '--scope', scope, '--json'],
      'auto',
      '智慧整理中。',
      { refresh: true }
    )
  }, [execute, scope])

  const restoreQuarantine = useCallback(async () => {
    if (!selectedBatch) return
    if (!window.confirm(`還原隔離批次 ${selectedBatch}？`)) return
    await execute(
      [
        '--restore-quarantine',
        selectedBatch,
        '--restore-conflict',
        conflictStrategy,
        '--json',
      ],
      'restore',
      `還原 ${selectedBatch} 中。`,
      { refresh: true }
    )
  }, [conflictStrategy, execute, selectedBatch])

  const purgeQuarantine = useCallback(async () => {
    if (!window.confirm(`清除超過 ${ttlHours} 小時且未釘選的隔離批次？`)) return
    await execute(
      ['--purge-quarantine', '--quarantine-ttl-hours', String(ttlHours), '--json'],
      'purge',
      '清除過期隔離批次中。',
      { refresh: true }
    )
  }, [execute, ttlHours])

  const toggleBatchPin = useCallback(async () => {
    const batch = batches.find((item) => item.name === selectedBatch)
    if (!batch?.name) return
    await execute(
      [batch.pinned ? '--unpin-quarantine' : '--pin-quarantine', batch.name, '--json'],
      'pin',
      `${batch.pinned ? '取消釘選' : '釘選'} ${batch.name} 中。`,
      { replaceResult: false, refresh: true }
    )
  }, [batches, execute, selectedBatch])

  const savePreferences = useCallback(async () => {
    await execute(
      [
        '--update-preferences',
        '--set-automation',
        automationEnabled ? 'enabled' : 'disabled',
        '--quarantine-ttl-hours',
        String(Math.max(1, ttlHours)),
        '--json',
      ],
      'preferences',
      '儲存清理規則偏好中。',
      { replaceResult: false, refresh: true }
    )
  }, [automationEnabled, execute, ttlHours])

  const cancelCurrentRun = useCallback(async () => {
    const cancelled = await cancelToolRun()
    setMessage(cancelled.message || '已要求取消目前操作。')
  }, [cancelToolRun])

  const items = result?.items || []
  const skipped = Array.isArray(result?.skipped) ? result.skipped : []
  const errors = result?.errors || []
  const filteredItems = useMemo(() => {
    const matching = items.filter((item) => {
      const riskOk = riskFilter === 'all' || item.risk === riskFilter
      const typeOk = typeFilter === 'all' || item.type === typeFilter
      return riskOk && typeOk
    })
    return [...matching].sort((left, right) => {
      if (sortMode === 'path') return String(left.path || '').localeCompare(String(right.path || ''))
      if (sortMode === 'risk') return String(right.risk || '').localeCompare(String(left.risk || ''))
      return Number(right.size_bytes || 0) - Number(left.size_bytes || 0)
    })
  }, [items, riskFilter, sortMode, typeFilter])

  const hasPlan = Boolean(result?.dry_run && result.plan_id && result.plan_token)
  const canApplyPlan = hasPlan && selectedItemIds.size > 0
  const canDirectDelete = canApplyPlan && result?.health?.direct_delete_allowed === true
  const activeHealth = result?.health || status?.quarantine_health
  const selectedBatchDetail = batches.find((batch) => batch.name === selectedBatch)
  const historyRows = status?.history || []

  const toggleItem = (itemId: string) => {
    setSelectedItemIds((current) => {
      const next = new Set(current)
      if (next.has(itemId)) next.delete(itemId)
      else next.add(itemId)
      return next
    })
  }

  const selectVisible = () => {
    setSelectedItemIds((current) => {
      const next = new Set(current)
      filteredItems.forEach((item) => item.item_id && next.add(item.item_id))
      return next
    })
  }

  return (
    <div className="project-cleaner">
      <header className="project-cleaner__header">
        <div>
          <p className="project-cleaner__eyebrow">Project Cleaner + System Rescue</p>
          <h1>專案清理與系統救援</h1>
          <p>v{status?.version || '1.0.0'} · 專案邊界 · 可復原修復 · 升級驗證</p>
        </div>
        <div className="project-cleaner__header-status">
          <span className={`project-cleaner__connection project-cleaner__connection--${socketStatus.toLowerCase()}`}>
            {busy ? progress?.message || '執行中' : socketStatus}
          </span>
          {busy ? (
            <button type="button" className="project-cleaner__cancel" onClick={() => void cancelCurrentRun()}>
              取消
            </button>
          ) : null}
        </div>
      </header>

      {progress ? (
        <div className="project-cleaner__progress" aria-live="polite">
          <div>
            <span>{progress.phase || 'prepare'}</span>
            <strong>{Math.max(0, Math.min(100, Number(progress.percent || 0)))}%</strong>
          </div>
          <progress max="100" value={Math.max(0, Math.min(100, Number(progress.percent || 0)))} />
          <p title={progress.current_path}>{progress.current_path || progress.message}</p>
        </div>
      ) : null}

      <main className="project-cleaner__layout">
        <section className="project-cleaner__panel project-cleaner__controls">
          <div className="project-cleaner__panel-head">
            <span>清理範圍</span>
            <strong>{scopeLabel(scope)}</strong>
          </div>
          <div className="project-cleaner__scope-grid">
            {SCOPE_OPTIONS.map((option) => (
              <button
                key={option.id}
                type="button"
                className={option.id === scope ? 'project-cleaner__scope project-cleaner__scope--active' : 'project-cleaner__scope'}
                disabled={busy}
                onClick={() => {
                  setScope(option.id)
                  setResult(null)
                  setSelectedItemIds(new Set())
                }}
              >
                <strong>{option.label}</strong>
                <span>{option.detail}</span>
              </button>
            ))}
          </div>
          <div className="project-cleaner__actions">
            <button type="button" disabled={busy} onClick={() => void previewCleanup()}>預覽</button>
            <button type="button" disabled={busy} onClick={() => void analyzeStorage()}>空間分析</button>
            <button type="button" disabled={busy} onClick={() => void systemRescueCheck()}>系統救援檢查</button>
            <button
              type="button"
              disabled={busy || socketStatus !== 'Connected'}
              onClick={() => void systemRescueRepair()}
              title={socketStatus === 'Connected' ? '' : '後端連線後才能執行系統救援修復'}
            >
              安全自動修復
            </button>
            <button type="button" className="project-cleaner__primary" disabled={busy || !canApplyPlan} onClick={() => void quarantineCleanup()}>
              隔離選取項目
            </button>
            <button type="button" disabled={busy} onClick={() => void automaticCleanup()}>智慧整理</button>
            <button type="button" className="project-cleaner__danger" disabled={busy || !canDirectDelete} onClick={() => void deleteCleanup()} title={canDirectDelete ? '' : '永久刪除只接受完整低風險預覽計畫'}>
              永久刪除
            </button>
          </div>
          <p className="project-cleaner__plan-state">
            {hasPlan
              ? `計畫 ${result?.plan_id?.slice(0, 8)} · ${selectedItemIds.size} / ${items.length} 已選取 · ${result?.plan_expires_in_minutes || 15} 分鐘內有效`
              : '尚未建立有效預覽計畫'}
          </p>
        </section>

        <section className="project-cleaner__panel project-cleaner__overview">
          <div className="project-cleaner__panel-head">
            <span>目前狀態</span>
            <strong>{healthTitle(activeHealth)}</strong>
          </div>
          <p className="project-cleaner__message">{message}</p>
          <div className="project-cleaner__health-grid">
            <div><span>安全</span><strong>{activeHealth?.safety_score ?? activeHealth?.score ?? '-'}</strong></div>
            <div><span>整潔</span><strong>{activeHealth?.cleanliness_score ?? '-'}</strong></div>
            <div><span>信心</span><strong>{activeHealth?.confidence_score ?? '-'}</strong></div>
          </div>
          <p className="project-cleaner__recommendation">{activeHealth?.recommendation || status?.quarantine_health?.recommendation}</p>
          <div className="project-cleaner__stats">
            <div><span>候選容量</span><strong>{formatBytes(result?.planned_bytes ?? result?.cleaned_bytes)}</strong></div>
            <div><span>檔案</span><strong>{result?.planned_files ?? result?.cleaned_files ?? 0}</strong></div>
            <div><span>資料夾</span><strong>{result?.planned_dirs ?? result?.cleaned_dirs ?? 0}</strong></div>
            <div><span>磁碟可用</span><strong>{status?.disk?.free_percent ?? '-'}%</strong></div>
          </div>
        </section>

        <section className="project-cleaner__panel project-cleaner__quarantine">
          <div className="project-cleaner__panel-head">
            <span>隔離區</span>
            <strong>{batches.length} 批</strong>
          </div>
          <div className="project-cleaner__batch-grid">
            <label>
              <span>批次</span>
              <select value={selectedBatch} disabled={busy || batches.length === 0} onChange={(event) => setSelectedBatch(event.target.value)}>
                {batches.length === 0 ? <option value="">沒有隔離批次</option> : batches.map((batch) => (
                  <option key={batch.name} value={batch.name}>
                    {batch.pinned ? '已釘選 · ' : ''}{batch.name} · {batch.restorable_count ?? batch.item_count ?? 0} 項 · {formatBytes(batch.size_bytes)}
                  </option>
                ))}
              </select>
            </label>
            <label>
              <span>衝突處理</span>
              <select value={conflictStrategy} disabled={busy} onChange={(event) => setConflictStrategy(event.target.value as 'skip' | 'rename')}>
                <option value="skip">保留現有檔案</option>
                <option value="rename">以新名稱還原</option>
              </select>
            </label>
          </div>
          <div className="project-cleaner__inline-actions">
            <button type="button" disabled={busy || !selectedBatch} onClick={() => void restoreQuarantine()}>還原</button>
            <button type="button" disabled={busy || !selectedBatch} onClick={() => void toggleBatchPin()}>{selectedBatchDetail?.pinned ? '取消釘選' : '釘選'}</button>
            <button type="button" disabled={busy} onClick={() => void purgeQuarantine()}>清除過期批次</button>
            <button type="button" disabled={busy} onClick={() => void refreshStatus()}>重新整理</button>
          </div>
          {selectedBatchDetail ? (
            <p className="project-cleaner__batch-meta">
              {selectedBatchDetail.status} · 建立 {formatDateTime(selectedBatchDetail.created_at)} · 到期 {formatDateTime(selectedBatchDetail.expires_at)}
              {selectedBatchDetail.recoverable ? ' · 可恢復中斷交易' : ''}
            </p>
          ) : null}
        </section>

        <section className="project-cleaner__panel project-cleaner__settings">
          <div className="project-cleaner__panel-head">
            <span>規則與自動化</span>
            <strong>Schema {status?.rules?.schema_version || 2}</strong>
          </div>
          <div className="project-cleaner__settings-grid">
            <label className="project-cleaner__toggle">
              <input type="checkbox" checked={automationEnabled} disabled={busy} onChange={(event) => setAutomationEnabled(event.target.checked)} />
              <span>啟用排程策略</span>
            </label>
            <label>
              <span>隔離保留小時</span>
              <input type="number" min="1" max="8760" value={ttlHours} disabled={busy} onChange={(event) => setTtlHours(Number(event.target.value || 1))} />
            </label>
            <button type="button" disabled={busy} onClick={() => void savePreferences()}>儲存設定</button>
          </div>
          <p className="project-cleaner__rules-path" title={status?.rules?.override_path}>
            {status?.rules?.directory_rule_count || 0} 個資料夾規則 · {status?.rules?.file_rule_count || 0} 個檔案規則 · {status?.rules?.override_exists ? '使用專案覆寫' : '使用內建規則'}
          </p>
        </section>

        <section className="project-cleaner__panel project-cleaner__results">
          <div className="project-cleaner__panel-head">
            <span>預覽項目</span>
            <strong>{filteredItems.length} / {items.length}</strong>
          </div>
          <div className="project-cleaner__selection-bar">
            <button type="button" disabled={!hasPlan || busy} onClick={selectVisible}>選取目前清單</button>
            <button type="button" disabled={!hasPlan || busy} onClick={() => setSelectedItemIds(new Set())}>清除選取</button>
            <span>{selectedItemIds.size} 項已選取</span>
          </div>
          <div className="project-cleaner__filters">
            <label><span>風險</span><select value={riskFilter} onChange={(event) => setRiskFilter(event.target.value)}><option value="all">全部</option><option value="low">低</option><option value="medium">中</option><option value="high">高</option></select></label>
            <label><span>類型</span><select value={typeFilter} onChange={(event) => setTypeFilter(event.target.value)}><option value="all">全部</option><option value="directory">資料夾</option><option value="file">檔案</option></select></label>
            <label><span>排序</span><select value={sortMode} onChange={(event) => setSortMode(event.target.value)}><option value="size-desc">容量</option><option value="path">路徑</option><option value="risk">風險</option></select></label>
          </div>
          <div className="project-cleaner__result-list">
            {filteredItems.length ? filteredItems.slice(0, 160).map((item) => {
              const itemId = String(item.item_id || '')
              return (
                <label key={itemId || item.path} className="project-cleaner__result-row">
                  <input type="checkbox" disabled={!itemId || busy || !hasPlan} checked={selectedItemIds.has(itemId)} onChange={() => toggleItem(itemId)} />
                  <div>
                    <strong title={item.path}>{item.path}</strong>
                    <span>{item.reason || item.rule_id} · 風險 {riskLabel(item.risk)} · {formatBytes(item.size_bytes)} · {item.age_days ?? 0} 天</span>
                  </div>
                  <em className={`project-cleaner__risk project-cleaner__risk--${item.risk || 'low'}`}>{riskLabel(item.risk)}</em>
                </label>
              )
            }) : <div className="project-cleaner__empty">目前沒有預覽項目。</div>}
          </div>
        </section>

        <section className="project-cleaner__panel project-cleaner__analysis">
          <div className="project-cleaner__panel-head">
            <span>空間分析</span>
            <strong>{analysis ? formatBytes(analysis.duplicate_wasted_bytes) : '尚未分析'}</strong>
          </div>
          <div className="project-cleaner__analysis-grid">
            <div>
              <h2>重複檔案</h2>
              {(analysis?.duplicate_groups || []).slice(0, 20).map((group) => (
                <div key={group.sha256} className="project-cleaner__analysis-row">
                  <strong>{group.copies} 份 · 可回收 {formatBytes(group.wasted_bytes)}</strong>
                  <span>{(group.paths || []).join(' · ')}</span>
                </div>
              ))}
              {!analysis?.duplicate_group_count ? <p>沒有重複檔案結果。</p> : null}
            </div>
            <div>
              <h2>大型舊檔</h2>
              {(analysis?.large_stale_files || []).slice(0, 20).map((file) => (
                <div key={file.path} className="project-cleaner__analysis-row">
                  <strong>{formatBytes(file.size_bytes)} · {file.age_days} 天</strong>
                  <span title={file.path}>{file.path}</span>
                </div>
              ))}
              {!analysis?.large_stale_count ? <p>沒有大型舊檔結果。</p> : null}
            </div>
          </div>
        </section>

        <section className="project-cleaner__panel project-cleaner__history">
          <div className="project-cleaner__panel-head">
            <span>操作歷史</span>
            <strong>{historyRows.length}</strong>
          </div>
          <div className="project-cleaner__history-list">
            {sessionHistory.map((entry) => <div key={`session-${entry}`}>{entry}</div>)}
            {historyRows.map((record) => <div key={record.operation_id || `${record.timestamp}-${record.action}`}>{historyText(record)}</div>)}
            {!sessionHistory.length && !historyRows.length ? <div>尚無操作紀錄。</div> : null}
          </div>
        </section>

        <section className="project-cleaner__panel project-cleaner__notices">
          <div className="project-cleaner__panel-head">
            <span>跳過與錯誤</span>
            <strong>{errors.length + skipped.length}</strong>
          </div>
          <div className="project-cleaner__notice-list">
            {[...errors, ...skipped].slice(0, 80).map((notice, index) => (
              <div key={`${notice.path}-${index}`}>
                <strong>{notice.path || notice.type || '項目'}</strong>
                <span>{notice.message || notice.reason || '已跳過'}</span>
                {notice.locked_by?.length ? <span>占用程序：{notice.locked_by.map((process) => `${process.name || '未知'} (PID ${process.pid || '-'})`).join('、')}</span> : null}
              </div>
            ))}
            {!errors.length && !skipped.length ? <div><strong>正常</strong><span>目前沒有清理錯誤或鎖檔項目。</span></div> : null}
          </div>
        </section>
      </main>
    </div>
  )
}
