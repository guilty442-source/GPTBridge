import { useEffect, useState } from 'react'
import {
  formatRunOutput,
  openFile,
  parseToolJson,
  toolWindowStyles as styles,
  useToolRunner,
} from './toolWindowRunner'

type RunState = 'idle' | 'running' | 'success' | 'error'

interface InvestmentQuoteReport {
  provider?: string
  price?: number
  currency?: string
  change_percent?: number | null
  market_state?: string
}

interface InvestmentHoldingReport {
  symbol?: string
  name?: string
  market?: string
  status?: string
  quote?: InvestmentQuoteReport | null
  market_value?: number | null
  unrealized_pnl?: number | null
  unrealized_pnl_percent?: number | null
}

interface InvestmentSnapshot {
  holding_count?: number
  quoted_count?: number
  holdings?: InvestmentHoldingReport[]
  market_summary?: {
    open_markets?: string[]
    all_watchable_markets_closed?: boolean
  }
}

interface InvestmentReport {
  stop_reason?: string
  latest_snapshot?: InvestmentSnapshot | null
  snapshots?: InvestmentSnapshot[]
}

interface InvestmentProgress {
  message?: string
  tool_id?: string
  snapshot?: InvestmentSnapshot
  phase?: string
}

function latestSnapshot(report: InvestmentReport | null): InvestmentSnapshot | null {
  if (!report) return null
  if (report.latest_snapshot) return report.latest_snapshot
  const snapshots = report.snapshots || []
  return snapshots.length > 0 ? snapshots[snapshots.length - 1] : null
}

function formatMoney(value: number | null | undefined, currency = ''): string {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '-'
  return `${currency ? `${currency} ` : ''}${value.toLocaleString('zh-TW', {
    maximumFractionDigits: Math.abs(value) >= 100 ? 2 : 4,
  })}`
}

function formatPercent(value: number | null | undefined): string {
  if (typeof value !== 'number' || !Number.isFinite(value)) return '-'
  return `${value >= 0 ? '+' : ''}${value.toFixed(2)}%`
}

export function InvestmentManagerWindowApp() {
  const { cancelToolRun, requestToolRun, socketStatus } = useToolRunner(
    'tool-mqi8uv5x-fo9f',
    60 * 60 * 1000
  )
  const [portfolioFile, setPortfolioFile] = useState('')
  const [watch, setWatch] = useState(true)
  const [intervalSeconds, setIntervalSeconds] = useState(60)
  const [runState, setRunState] = useState<RunState>('idle')
  const [message, setMessage] = useState('投資管家已就緒')
  const [output, setOutput] = useState('')
  const [snapshot, setSnapshot] = useState<InvestmentSnapshot | null>(null)
  const [stopRequested, setStopRequested] = useState(false)

  useEffect(() => {
    const handler = (event: Event) => {
      const customEvent = event as CustomEvent
      const detail = customEvent.detail || {}
      if (detail.event !== 'toolbox_run_tool_progress') return
      const payload = (detail.payload || {}) as InvestmentProgress
      if (payload.tool_id && payload.tool_id !== 'tool-mqi8uv5x-fo9f') return
      if (payload.message) setMessage(payload.message)
      if (payload.snapshot) setSnapshot(payload.snapshot)
      if (payload.phase === 'market_closed') {
        setMessage('市場已收盤，已停止持續更新。')
      }
    }
    window.addEventListener('ipc_event', handler)
    return () => window.removeEventListener('ipc_event', handler)
  }, [])

  const choosePortfolio = async () => {
    const file = await openFile()
    if (file) {
      setPortfolioFile(file)
      setSnapshot(null)
      setOutput('')
    }
  }

  const run = () => {
    if (!portfolioFile.trim() || runState === 'running') return
    const args = ['--portfolio', portfolioFile.trim(), '--json', '--progress-jsonl']
    if (watch) args.push('--watch', '--interval', String(intervalSeconds))
    setRunState('running')
    setMessage('正在連線多來源報價...')
    setOutput('')
    setSnapshot(null)
    setStopRequested(false)
    void (async () => {
      try {
        const result = await requestToolRun(args)
        const report = parseToolJson<InvestmentReport>(result.stdout)
        const latest = latestSnapshot(report)
        if (latest) setSnapshot(latest)
        if (result.cancelled) {
          setRunState('idle')
          setMessage('投資監控已停止。')
          return
        }
        const ok = result.ok !== false
        setRunState(ok ? 'success' : 'error')
        setMessage(
          ok
            ? report?.stop_reason === 'market_closed'
              ? '市場已收盤，已停止持續更新。'
              : `報價完成：${latest?.quoted_count ?? 0}/${latest?.holding_count ?? 0}`
            : result.message || '報價失敗'
        )
        setOutput(formatRunOutput(result))
      } catch (error) {
        setRunState('error')
        setMessage(error instanceof Error ? error.message : '投資管家執行逾時')
      } finally {
        setStopRequested(false)
      }
    })()
  }

  const stop = () => {
    if (runState !== 'running' || stopRequested) return
    setStopRequested(true)
    setMessage('正在停止投資監控...')
    void cancelToolRun().catch((error) => {
      setStopRequested(false)
      setMessage(error instanceof Error ? error.message : '停止投資監控失敗')
    })
  }

  return (
    <main style={styles.app}>
      <section style={styles.card}>
        <header style={styles.header}>
          <div>
            <div style={styles.kicker}>Standalone Application</div>
            <h1 style={styles.title}>投資管家</h1>
            <p style={styles.muted}>依照匯入庫存檔，以多來源連網監控報價；收盤後停止持續更新。</p>
          </div>
          <span style={styles.badge}>{socketStatus}</span>
        </header>

        <div style={styles.fieldGroup}>
          <label style={styles.label}>庫存檔案</label>
          <div style={styles.inlineRow}>
            <input
              value={portfolioFile}
              onChange={(event) => setPortfolioFile(event.target.value)}
              placeholder="選擇 CSV、TSV 或 JSON 庫存檔"
              style={styles.input}
            />
            <button type="button" onClick={choosePortfolio} style={styles.secondaryButton}>
              選擇檔案
            </button>
          </div>
        </div>

        <section style={styles.notice}>
          <label style={styles.checkboxRow}>
            <input type="checkbox" checked={watch} onChange={(event) => setWatch(event.target.checked)} />
            <strong>即時監控直到市場收盤</strong>
          </label>
          <label style={{ ...styles.sliderControl, marginTop: 12 }}>
            <span style={styles.sliderHeader}>
              <span>更新間隔</span>
              <strong>{intervalSeconds}s</strong>
            </span>
            <input
              type="range"
              min={15}
              max={300}
              step={15}
              value={intervalSeconds}
              onChange={(event) => setIntervalSeconds(Number(event.target.value))}
              style={styles.rangeInput}
            />
          </label>
        </section>

        <div style={styles.actions}>
          {runState === 'running' ? (
            <button type="button" onClick={stop} disabled={stopRequested} style={styles.dangerButton}>
              {stopRequested ? '停止中...' : '停止監控'}
            </button>
          ) : null}
          <button
            type="button"
            onClick={run}
            disabled={!portfolioFile.trim() || runState === 'running'}
            style={styles.primaryButton}
          >
            {runState === 'running' ? '執行中...' : '開始監控'}
          </button>
        </div>

        {snapshot ? (
          <section style={styles.resultPanel}>
            <strong>
              報價 {snapshot.quoted_count ?? 0}/{snapshot.holding_count ?? 0}
            </strong>
            <span style={styles.resultMeta}>
              {snapshot.market_summary?.all_watchable_markets_closed
                ? '市場已收盤'
                : `開盤市場：${snapshot.market_summary?.open_markets?.join(', ') || '-'}`}
            </span>
            <div style={styles.resultList}>
              {(snapshot.holdings || []).map((holding) => {
                const quote = holding.quote || null
                const currency = quote?.currency || ''
                return (
                  <div key={`${holding.market || ''}:${holding.symbol || ''}`} style={styles.resultRow}>
                    <span style={styles.resultText}>
                      <span style={styles.resultPath}>
                        {holding.symbol}
                        {holding.name ? ` - ${holding.name}` : ''}
                      </span>
                      <span style={styles.resultMeta}>
                        {holding.market || '-'} / {holding.status || '-'} / 現價 {formatMoney(quote?.price, currency)} / 市值{' '}
                        {formatMoney(holding.market_value, currency)} / 損益{' '}
                        {formatMoney(holding.unrealized_pnl, currency)} ({formatPercent(holding.unrealized_pnl_percent)})
                      </span>
                    </span>
                  </div>
                )
              })}
            </div>
          </section>
        ) : null}

        <section style={styles.statusPanel}>
          <div style={styles.statusLine}>
            <span
              style={{
                ...styles.statusDot,
                background:
                  runState === 'success'
                    ? '#34d399'
                    : runState === 'error'
                      ? '#f87171'
                      : runState === 'running'
                        ? '#fbbf24'
                        : '#64748b',
              }}
            />
            <strong>{message}</strong>
          </div>
          <pre style={styles.output}>{output || '尚無輸出。'}</pre>
        </section>
      </section>
    </main>
  )
}
