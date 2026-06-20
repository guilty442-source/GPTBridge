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
  | 'dry-run'
  | 'quarantine'
  | 'delete'
  | 'purge'
  | 'restore'

interface CleanupItem {
  path?: string
  type?: string
  size_bytes?: number
  reason?: string
  risk?: string
  age_days?: number
  min_age_days?: number
}

interface CleanupNotice {
  path?: string
  reason?: string
  message?: string
  type?: string
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
  ttl_hours?: number
  scope?: string
}

interface CleanupResult {
  ok?: boolean
  scope?: string
  dry_run?: boolean
  quarantine?: boolean
  cleaned_files?: number
  cleaned_dirs?: number
  cleaned_bytes?: number
  planned_files?: number
  planned_dirs?: number
  planned_bytes?: number
  restored?: number
  purged_dirs?: number
  purged_bytes?: number
  quarantine_path?: string
  quarantine_manifest?: string
  summary?: CleanupSummary
  health?: CleanupHealth
  items?: CleanupItem[]
  skipped?: CleanupNotice[]
  errors?: CleanupNotice[]
  skipped_count?: number
  error_count?: number
  message?: string
}

interface QuarantineBatch {
  name?: string
  path?: string
  created_at?: string
  scope?: string
  item_count?: number
  size_bytes?: number
  age_hours?: number
  expires_at?: string
  expired?: boolean
  manifest_path?: string
}

interface QuarantineListResult {
  ok?: boolean
  batches?: QuarantineBatch[]
  batch_count?: number
  total_bytes?: number
  health?: CleanupHealth
  message?: string
}

interface CleanerStatus {
  ok?: boolean
  version?: string
  project_root?: string
  quarantine?: QuarantineListResult
  quarantine_health?: CleanupHealth
  message?: string
}

const SCOPE_OPTIONS: Array<{
  id: CleanupScope
  label: string
  detail: string
}> = [
  {
    id: 'global',
    label: '整個專案',
    detail: '掃描專案內的快取、暫存與過期輸出。',
  },
  {
    id: 'runtime',
    label: 'Runtime',
    detail: '清理執行期暫存，保留登入 Profile 與狀態資料。',
  },
  {
    id: 'sandbox',
    label: 'Sandbox',
    detail: '清理 RuntimeSandbox 內的工作檔與快取。',
  },
]

function scopeLabel(scope: string | undefined): string {
  return SCOPE_OPTIONS.find((item) => item.id === scope)?.label || '整個專案'
}

function formatBytes(value: number | undefined): string {
  const bytes = Number(value ?? 0)
  if (!Number.isFinite(bytes) || bytes <= 0) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let size = bytes
  let unitIndex = 0
  while (size >= 1024 && unitIndex < units.length - 1) {
    size /= 1024
    unitIndex += 1
  }
  return `${size >= 10 || unitIndex === 0 ? size.toFixed(0) : size.toFixed(1)} ${units[unitIndex]}`
}

function connectionTone(status: string): string {
  if (status === 'Connected') return 'project-cleaner__connection--connected'
  if (status === 'Error') return 'project-cleaner__connection--error'
  return ''
}

function resultTitle(result: CleanupResult | null): string {
  if (!result) return '等待預覽'
  if (result.ok === false) return '執行失敗'
  if (typeof result.restored === 'number') return '隔離已還原'
  if (typeof result.purged_dirs === 'number') return '隔離區已清理'
  if (result.dry_run) return '清理計畫'
  return result.quarantine ? '已移入隔離區' : '清理完成'
}

function resultMessage(result: CleanupResult | null): string {
  if (!result) return '尚未產生清理計畫。'
  if (result.ok === false) return result.message || '清理工具執行失敗。'
  if (typeof result.restored === 'number') {
    return `已還原 ${result.restored} 個隔離項目。`
  }
  if (typeof result.purged_dirs === 'number') {
    return `已清理 ${result.purged_dirs} 個過期隔離批次，釋放 ${formatBytes(result.purged_bytes)}。`
  }
  const prefix = result.dry_run
    ? '預計處理'
    : result.quarantine
      ? '已隔離'
      : '已刪除'
  return `${prefix} ${formatBytes(result.cleaned_bytes)}，檔案 ${result.cleaned_files ?? 0}，資料夾 ${
    result.cleaned_dirs ?? 0
  }。`
}

function riskLabel(risk: string | undefined): string {
  if (risk === 'medium') return '中'
  if (risk === 'high') return '高'
  return '低'
}

function healthTitle(health: CleanupHealth | undefined): string {
  if (!health) return '尚未評估'
  if (health.state === 'clean') return '乾淨'
  if (health.state === 'attention') return '需處理'
  if (health.state === 'review') return '需檢查'
  if (health.state === 'ready') return '可清理'
  if (health.state === 'healthy') return '健康'
  if (health.state === 'empty') return '空'
  return '待命'
}

function healthTone(health: CleanupHealth | undefined): string {
  if (!health) return ''
  if (health.state === 'attention' || health.safety_level === 'high') {
    return 'project-cleaner__health--bad'
  }
  if (health.state === 'review' || health.safety_level === 'medium') {
    return 'project-cleaner__health--warn'
  }
  return 'project-cleaner__health--ok'
}

function formatDateTime(value: string | undefined): string {
  if (!value) return '-'
  const timestamp = Date.parse(value)
  if (!Number.isFinite(timestamp)) return value
  return new Date(timestamp).toLocaleString('zh-TW', { hour12: false })
}

export function ProjectCleanerWindowApp() {
  const { requestToolRun, socketStatus } = useToolRunner(
    'project-cleaner',
    15 * 60 * 1000
  )
  const [scope, setScope] = useState<CleanupScope>('runtime')
  const [mode, setMode] = useState<CleanupMode>('idle')
  const [message, setMessage] = useState('清理工具已就緒。')
  const [result, setResult] = useState<CleanupResult | null>(null)
  const [quarantineBatches, setQuarantineBatches] = useState<QuarantineBatch[]>([])
  const [cleanerStatus, setCleanerStatus] = useState<CleanerStatus | null>(null)
  const [selectedBatch, setSelectedBatch] = useState('')
  const [riskFilter, setRiskFilter] = useState('all')
  const [typeFilter, setTypeFilter] = useState('all')
  const [sortMode, setSortMode] = useState('size-desc')
  const [history, setHistory] = useState<string[]>([])

  const busy = mode !== 'idle'
  const statusText = useMemo(() => {
    if (mode === 'dry-run') return '預覽中'
    if (mode === 'quarantine') return '隔離中'
    if (mode === 'delete') return '刪除中'
    if (mode === 'purge') return '整理中'
    if (mode === 'restore') return '還原中'
    return socketStatus
  }, [mode, socketStatus])

  const appendHistory = useCallback((text: string) => {
    const clock = new Date().toLocaleTimeString('zh-TW', { hour12: false })
    setHistory((current) => [`${clock} | ${text}`, ...current].slice(0, 14))
  }, [])

  const refreshQuarantineBatches = useCallback(async () => {
    try {
      const runResult = await requestToolRun(['--status', '--json'])
      const payload = parseToolJson<CleanerStatus>(runResult.stdout)
      const batches = payload?.quarantine?.batches || []
      setCleanerStatus(payload || null)
      setQuarantineBatches(batches)
      setSelectedBatch((current) =>
        current && batches.some((batch) => batch.name === current)
          ? current
          : batches[0]?.name || ''
      )
    } catch {
      setCleanerStatus(null)
      setQuarantineBatches([])
      setSelectedBatch('')
    }
  }, [requestToolRun])

  useEffect(() => {
    void refreshQuarantineBatches()
  }, [refreshQuarantineBatches])

  const runTool = useCallback(
    async (args: string[], nextMode: CleanupMode, startText: string) => {
      setMode(nextMode)
      setResult(null)
      setMessage(startText)
      appendHistory(startText)

      try {
        const runResult = await requestToolRun(args)
        const payload =
          parseToolJson<CleanupResult>(runResult.stdout) ?? ({
            ok: runResult.ok,
            message: formatRunOutput(runResult) || runResult.message,
          } as CleanupResult)
        setResult(payload)
        const nextMessage = resultMessage(payload)
        setMessage(nextMessage)
        appendHistory(nextMessage)
        if (nextMode === 'quarantine' || nextMode === 'purge' || nextMode === 'restore') {
          void refreshQuarantineBatches()
        }
      } catch (error) {
        const nextMessage =
          error instanceof Error ? error.message : '清理工具執行時發生未知錯誤。'
        const payload = {
          ok: false,
          scope,
          message: nextMessage,
        }
        setResult(payload)
        setMessage(nextMessage)
        appendHistory(nextMessage)
      } finally {
        setMode('idle')
      }
    },
    [appendHistory, refreshQuarantineBatches, requestToolRun, scope]
  )

  const previewCleanup = useCallback(() => {
    void runTool(
      ['--cleanup-garbage', '--scope', scope, '--dry-run', '--json'],
      'dry-run',
      `${scopeLabel(scope)}清理預覽中。`
    )
  }, [runTool, scope])

  const quarantineCleanup = useCallback(() => {
    void runTool(
      ['--cleanup-garbage', '--scope', scope, '--quarantine', '--json'],
      'quarantine',
      `${scopeLabel(scope)}清理執行中，項目會先移入隔離區。`
    )
  }, [runTool, scope])

  const deleteCleanup = useCallback(() => {
    if (!window.confirm(`直接刪除 ${scopeLabel(scope)} 的清理項目？`)) return
    void runTool(
      ['--cleanup-garbage', '--scope', scope, '--json'],
      'delete',
      `${scopeLabel(scope)}直接清理中。`
    )
  }, [runTool, scope])

  const purgeQuarantine = useCallback(() => {
    if (!window.confirm('清理超過 24 小時的隔離批次？')) return
    void runTool(
      ['--purge-quarantine', '--json'],
      'purge',
      '整理過期隔離批次中。'
    )
  }, [runTool])

  const restoreQuarantine = useCallback(() => {
    const defaultBatch = result?.quarantine_path
      ? result.quarantine_path.split(/[\\/]/).filter(Boolean).pop() || ''
      : ''
    const trimmed = (selectedBatch || defaultBatch).trim()
    if (!trimmed) {
      setMessage('尚未選擇可還原的隔離批次。')
      return
    }
    if (!window.confirm(`還原隔離批次 ${trimmed}？`)) return
    void runTool(
      ['--restore-quarantine', trimmed, '--json'],
      'restore',
      `還原隔離批次 ${trimmed} 中。`
    )
  }, [result?.quarantine_path, runTool, selectedBatch])

  const items = result?.items ?? []
  const skipped = result?.skipped ?? []
  const errors = result?.errors ?? []
  const totalQuarantineBytes = useMemo(
    () =>
      quarantineBatches.reduce(
        (total, batch) => total + Number(batch.size_bytes || 0),
        0
      ),
    [quarantineBatches]
  )
  const riskBuckets = result?.summary?.by_risk || {}
  const reasonBuckets = result?.summary?.by_reason || {}
  const largestItems = result?.summary?.largest_items || []
  const activeHealth = result?.health || cleanerStatus?.quarantine_health
  const canDirectDelete =
    Boolean(result?.dry_run) && result?.health?.direct_delete_allowed === true
  const filteredItems = useMemo(() => {
    const nextItems = items.filter((item) => {
      const riskOk = riskFilter === 'all' || item.risk === riskFilter
      const typeOk = typeFilter === 'all' || item.type === typeFilter
      return riskOk && typeOk
    })
    return [...nextItems].sort((a, b) => {
      if (sortMode === 'path') return String(a.path || '').localeCompare(String(b.path || ''))
      if (sortMode === 'risk') return String(b.risk || '').localeCompare(String(a.risk || ''))
      return Number(b.size_bytes || 0) - Number(a.size_bytes || 0)
    })
  }, [items, riskFilter, sortMode, typeFilter])
  const selectedBatchDetail = quarantineBatches.find(
    (batch) => batch.name === selectedBatch
  )

  return (
    <div className="project-cleaner">
      <header className="project-cleaner__header">
        <div>
          <p className="project-cleaner__eyebrow">Application</p>
          <h1>清理工具</h1>
          <p>v{cleanerStatus?.version || '1.2.0'} · 以清理計畫、TTL 與隔離區管理專案暫存。</p>
        </div>
        <span
          className={`project-cleaner__connection ${connectionTone(socketStatus)}`}
        >
          {statusText}
        </span>
      </header>

      <main className="project-cleaner__layout">
        <section className="project-cleaner__panel">
          <div className="project-cleaner__panel-head">
            <span>清理範圍</span>
            <strong>{scopeLabel(scope)}</strong>
          </div>
          <div className="project-cleaner__scope-grid">
            {SCOPE_OPTIONS.map((option) => (
              <button
                key={option.id}
                type="button"
                className={
                  option.id === scope
                    ? 'project-cleaner__scope project-cleaner__scope--active'
                    : 'project-cleaner__scope'
                }
                disabled={busy}
                onClick={() => setScope(option.id)}
              >
                <strong>{option.label}</strong>
                <span>{option.detail}</span>
              </button>
            ))}
          </div>

          <div className="project-cleaner__actions">
            <button type="button" disabled={busy} onClick={previewCleanup}>
              {mode === 'dry-run' ? '預覽中...' : '預覽'}
            </button>
            <button
              type="button"
              className="project-cleaner__primary"
              disabled={busy}
              onClick={quarantineCleanup}
            >
              {mode === 'quarantine' ? '隔離中...' : '隔離清理'}
            </button>
            <button type="button" disabled={busy} onClick={purgeQuarantine}>
              {mode === 'purge' ? '整理中...' : '清理隔離區'}
            </button>
            <button type="button" disabled={busy || !selectedBatch} onClick={restoreQuarantine}>
              {mode === 'restore' ? '還原中...' : '還原隔離'}
            </button>
            <button
              type="button"
              className="project-cleaner__danger"
              disabled={busy || !canDirectDelete}
              onClick={deleteCleanup}
              title={canDirectDelete ? '' : '直接刪除需先完成低風險預覽'}
            >
              {mode === 'delete' ? '刪除中...' : '直接刪除'}
            </button>
          </div>
          <div className="project-cleaner__batch-picker">
            <label>
              <span>隔離批次</span>
              <select
                value={selectedBatch}
                disabled={busy || quarantineBatches.length === 0}
                onChange={(event) => setSelectedBatch(event.target.value)}
              >
                {quarantineBatches.length === 0 ? (
                  <option value="">尚無可還原批次</option>
                ) : (
                  quarantineBatches.map((batch) => (
                    <option key={batch.name} value={batch.name}>
                      {batch.name} | {batch.item_count ?? 0} 項 | {formatBytes(batch.size_bytes)}
                      {batch.expired ? ' | 已過期' : ''}
                    </option>
                  ))
                )}
              </select>
            </label>
            <button type="button" disabled={busy} onClick={() => void refreshQuarantineBatches()}>
              更新批次
            </button>
          </div>
        </section>

        <section className="project-cleaner__status">
          <div className="project-cleaner__status-head">
            <span>狀態</span>
            <strong>{resultTitle(result)}</strong>
          </div>
          <p>{message}</p>
          <div className={`project-cleaner__health ${healthTone(activeHealth)}`}>
            <div>
              <span>健康狀態</span>
              <strong>{healthTitle(activeHealth)}</strong>
            </div>
            <div>
              <span>安全分數</span>
              <strong>{activeHealth?.score ?? '-'}</strong>
            </div>
            <p>{activeHealth?.recommendation || '等待清理預覽或隔離區狀態。'}</p>
          </div>
          <div className="project-cleaner__stats">
            <div>
              <span>容量</span>
              <strong>{formatBytes(result?.cleaned_bytes ?? result?.planned_bytes)}</strong>
            </div>
            <div>
              <span>檔案</span>
              <strong>{result?.cleaned_files ?? result?.planned_files ?? 0}</strong>
            </div>
            <div>
              <span>資料夾</span>
              <strong>{result?.cleaned_dirs ?? result?.planned_dirs ?? 0}</strong>
            </div>
            <div>
              <span>隔離批次</span>
              <strong>{quarantineBatches.length}</strong>
            </div>
            <div>
              <span>過期批次</span>
              <strong>{cleanerStatus?.quarantine_health?.expired_count ?? 0}</strong>
            </div>
          </div>
          <div className="project-cleaner__summary-grid">
            {Object.entries(riskBuckets).map(([risk, bucket]) => (
              <div key={risk}>
                <span>風險 {riskLabel(risk)}</span>
                <strong>{bucket.count ?? 0}</strong>
                <em>{formatBytes(bucket.size_bytes)}</em>
              </div>
            ))}
            {selectedBatchDetail ? (
              <div>
                <span>選取批次</span>
                <strong>{selectedBatchDetail.item_count ?? 0}</strong>
                <em>{formatDateTime(selectedBatchDetail.created_at)}</em>
              </div>
            ) : (
              <div>
                <span>隔離容量</span>
                <strong>{formatBytes(totalQuarantineBytes)}</strong>
                <em>可還原批次總量</em>
              </div>
            )}
          </div>
          {result?.quarantine_path ? (
            <div className="project-cleaner__path" title={result.quarantine_path}>
              {result.quarantine_path}
            </div>
          ) : null}
        </section>

        <section className="project-cleaner__results">
          <div className="project-cleaner__panel-head">
            <span>清理項目</span>
            <strong>{filteredItems.length} / {items.length}</strong>
          </div>
          <div className="project-cleaner__filters">
            <label>
              <span>風險</span>
              <select value={riskFilter} onChange={(event) => setRiskFilter(event.target.value)}>
                <option value="all">全部</option>
                <option value="low">低</option>
                <option value="medium">中</option>
                <option value="high">高</option>
              </select>
            </label>
            <label>
              <span>類型</span>
              <select value={typeFilter} onChange={(event) => setTypeFilter(event.target.value)}>
                <option value="all">全部</option>
                <option value="directory">資料夾</option>
                <option value="file">檔案</option>
              </select>
            </label>
            <label>
              <span>排序</span>
              <select value={sortMode} onChange={(event) => setSortMode(event.target.value)}>
                <option value="size-desc">容量大到小</option>
                <option value="path">路徑</option>
                <option value="risk">風險</option>
              </select>
            </label>
          </div>
          <div className="project-cleaner__result-list">
            {filteredItems.length > 0 ? (
              filteredItems.slice(0, 120).map((item, index) => (
                <div key={`${item.path}-${index}`} className="project-cleaner__result-row">
                  <div>
                    <strong>{item.path}</strong>
                    <span>
                      {item.reason || item.type} | 風險 {riskLabel(item.risk)} | {formatBytes(item.size_bytes)}
                    </span>
                  </div>
                  <em>{item.type === 'directory' ? '資料夾' : '檔案'}</em>
                </div>
              ))
            ) : (
              <div className="project-cleaner__empty">尚無清理項目。</div>
            )}
          </div>
          {largestItems.length > 0 ? (
            <div className="project-cleaner__largest">
              <span>最大項目</span>
              {largestItems.slice(0, 3).map((item) => (
                <strong key={item.path}>{item.path} · {formatBytes(item.size_bytes)}</strong>
              ))}
            </div>
          ) : null}
        </section>

        <section className="project-cleaner__history">
          <div className="project-cleaner__panel-head">
            <span>紀錄</span>
            <strong>{history.length}</strong>
          </div>
          <div className="project-cleaner__history-list">
            {history.length > 0 ? (
              history.map((entry) => <div key={entry}>{entry}</div>)
            ) : (
              <div>尚無執行紀錄。</div>
            )}
          </div>
        </section>

        <section className="project-cleaner__notices">
          <div className="project-cleaner__panel-head">
            <span>略過與錯誤</span>
            <strong>{(result?.skipped_count ?? skipped.length) + (result?.error_count ?? errors.length)}</strong>
          </div>
          {Object.keys(reasonBuckets).length > 0 ? (
            <div className="project-cleaner__reason-grid">
              {Object.entries(reasonBuckets).slice(0, 6).map(([reason, bucket]) => (
                <div key={reason}>
                  <strong>{reason}</strong>
                  <span>{bucket.count ?? 0} 項 · {formatBytes(bucket.size_bytes)}</span>
                </div>
              ))}
            </div>
          ) : null}
          <div className="project-cleaner__notice-list">
            {[...errors, ...skipped].slice(0, 40).map((item, index) => (
              <div key={`${item.path}-${index}`}>
                <strong>{item.path || item.type || '項目'}</strong>
                <span>{item.message || item.reason || '已略過'}</span>
              </div>
            ))}
            {errors.length === 0 && skipped.length === 0 ? (
              <div>
                <strong>無</strong>
                <span>目前沒有錯誤或略過項目。</span>
              </div>
            ) : null}
          </div>
        </section>
      </main>
    </div>
  )
}
