import { useCallback, useMemo, useState } from 'react'
import {
  formatRunOutput,
  parseToolJson,
  useToolRunner,
} from './toolWindowRunner'
import './project-cleaner.css'

type CleanupScope = 'global' | 'runtime' | 'sandbox'
type CleanupMode = 'idle' | 'dry-run' | 'cleanup'

interface CleanupResult {
  ok?: boolean
  scope?: string
  dry_run?: boolean
  cleaned_files?: number
  cleaned_dirs?: number
  cleaned_bytes?: number
  message?: string
}

const SCOPE_OPTIONS: Array<{
  id: CleanupScope
  label: string
  detail: string
}> = [
  {
    id: 'global',
    label: '完整專案',
    detail: '掃描整個 GPTBridge 專案，清理可安全移除的快取、暫存與輸出。',
  },
  {
    id: 'runtime',
    label: '執行資料',
    detail: '清理 runtime 暫存資料，保留瀏覽器登入 Profile。',
  },
  {
    id: 'sandbox',
    label: '沙盒',
    detail: '清理 RuntimeSandbox 暫存與測試輸出。',
  },
]

function scopeLabel(scope: string | undefined): string {
  return SCOPE_OPTIONS.find((item) => item.id === scope)?.label || '完整專案'
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
  if (!result) return '等待執行'
  if (result.ok === false) return '清理失敗'
  return result.dry_run ? '預覽完成' : '清理完成'
}

function resultMessage(result: CleanupResult | null): string {
  if (!result) return '選擇範圍後可先預覽，再執行清理。'
  if (result.ok === false) return result.message || '清理工具執行失敗'
  const prefix = result.dry_run ? '可清理' : '已清理'
  return `${prefix} ${formatBytes(result.cleaned_bytes)}，檔案 ${result.cleaned_files ?? 0}，資料夾 ${result.cleaned_dirs ?? 0}`
}

export function ProjectCleanerWindowApp() {
  const { requestToolRun, socketStatus } = useToolRunner(
    'project-cleaner',
    15 * 60 * 1000
  )
  const [scope, setScope] = useState<CleanupScope>('global')
  const [mode, setMode] = useState<CleanupMode>('idle')
  const [message, setMessage] = useState('清理工具已就緒')
  const [result, setResult] = useState<CleanupResult | null>(null)
  const [history, setHistory] = useState<string[]>([])

  const busy = mode !== 'idle'
  const statusText = useMemo(() => {
    if (mode === 'dry-run') return '預覽中'
    if (mode === 'cleanup') return '清理中'
    return socketStatus
  }, [mode, socketStatus])

  const appendHistory = useCallback((text: string) => {
    const clock = new Date().toLocaleTimeString('zh-TW', { hour12: false })
    setHistory((current) => [`${clock} | ${text}`, ...current].slice(0, 12))
  }, [])

  const runCleanup = useCallback(
    async (dryRun: boolean) => {
      const nextMode: CleanupMode = dryRun ? 'dry-run' : 'cleanup'
      setMode(nextMode)
      setResult(null)
      setMessage(
        dryRun
          ? `${scopeLabel(scope)}預覽中，正在估算可清理內容...`
          : `${scopeLabel(scope)}清理中，請保持視窗開啟...`
      )
      appendHistory(`${scopeLabel(scope)} - ${dryRun ? '開始預覽' : '開始清理'}`)

      try {
        const args = ['--cleanup-garbage', '--scope', scope, '--json']
        if (dryRun) args.push('--dry-run')
        const runResult = await requestToolRun(args)
        const payload =
          parseToolJson<CleanupResult>(runResult.stdout) ?? ({
            ok: runResult.ok,
            message: formatRunOutput(runResult) || runResult.message,
          } as CleanupResult)
        setResult(payload)
        const nextMessage = resultMessage(payload)
        setMessage(nextMessage)
        appendHistory(`${scopeLabel(payload.scope || scope)} - ${nextMessage}`)
      } catch (error) {
        const nextMessage =
          error instanceof Error ? error.message : '清理工具執行逾時'
        const payload = {
          ok: false,
          scope,
          dry_run: dryRun,
          message: nextMessage,
        }
        setResult(payload)
        setMessage(nextMessage)
        appendHistory(`${scopeLabel(scope)} - ${nextMessage}`)
      } finally {
        setMode('idle')
      }
    },
    [appendHistory, requestToolRun, scope]
  )

  return (
    <div className="project-cleaner">
      <header className="project-cleaner__header">
        <div>
          <p className="project-cleaner__eyebrow">Application</p>
          <h1>清理工具</h1>
          <p>清理規則與執行邏輯由 project-cleaner 獨立專案提供。</p>
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
            <button type="button" disabled={busy} onClick={() => runCleanup(true)}>
              {mode === 'dry-run' ? '預覽中...' : '先預覽'}
            </button>
            <button
              type="button"
              className="project-cleaner__primary"
              disabled={busy}
              onClick={() => runCleanup(false)}
            >
              {mode === 'cleanup' ? '清理中...' : '執行清理'}
            </button>
          </div>
        </section>

        <section className="project-cleaner__status">
          <div className="project-cleaner__status-head">
            <span>狀態</span>
            <strong>{resultTitle(result)}</strong>
          </div>
          <p>{message}</p>
          <div className="project-cleaner__stats">
            <div>
              <span>容量</span>
              <strong>{formatBytes(result?.cleaned_bytes)}</strong>
            </div>
            <div>
              <span>檔案</span>
              <strong>{result?.cleaned_files ?? 0}</strong>
            </div>
            <div>
              <span>資料夾</span>
              <strong>{result?.cleaned_dirs ?? 0}</strong>
            </div>
          </div>
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
              <div>尚無清理紀錄</div>
            )}
          </div>
        </section>
      </main>
    </div>
  )
}
