import React, { useState } from 'react'
import { useCommandQuery } from '../hooks'
import { DataFreshnessIndicator } from '../freshness'
import { EmptyState, Section, Badge } from '../common'

type Props = { sendCommand: (c: string, p?: unknown) => { ok: boolean; message?: string } }

const arr = (v: unknown): Record<string, unknown>[] =>
  Array.isArray(v) ? (v as Record<string, unknown>[]) : []

const KINDS = [
  { key: '', label: '全部' },
  { key: 'daily', label: '每日' },
  { key: 'weekly', label: '每週' },
  { key: 'monthly', label: '每月' },
  { key: 'strategy', label: '策略' },
  { key: 'fund', label: '基金分析' },
  { key: 'allocation', label: '資產配置' },
]

export default function ReportPage({ sendCommand }: Props) {
  const [kind, setKind] = useState('')
  const [q, setQ] = useState('')
  const [open, setOpen] = useState<Record<string, unknown> | null>(null)

  const monitor = useCommandQuery<any>(
    sendCommand, 'investment_monitor_reports', {}, [kind])
  const autotrade = useCommandQuery<any>(
    sendCommand, 'investment_autotrade_reports')

  const all = [
    ...arr(monitor.data?.reports),
    ...arr(autotrade.data?.reports),
  ]
  const rows = all.filter((r) => {
    if (kind && !String(r.report_type || r.kind || '').toLowerCase().includes(kind))
      return false
    if (q && !JSON.stringify(r).toLowerCase().includes(q.toLowerCase()))
      return false
    return true
  })

  return (
    <div className="inv-page">
      <header className="inv-page-head">
        <h2>投資報告中心</h2>
        <div className="inv-filter">
          {KINDS.map((k) => (
            <button key={k.key} className={kind === k.key ? 'active' : ''}
              onClick={() => setKind(k.key)}>{k.label}</button>
          ))}
          <input value={q} placeholder="搜尋…"
            onChange={(e) => setQ(e.target.value)} />
        </div>
      </header>

      <div className="inv-cards">
        {rows.map((r, i) => (
          <article key={i} className="inv-card">
            <header className="inv-card-head">
              <b>{String(r.title || r.report_id || `報告 ${i + 1}`)}</b>
              <Badge text={String(r.report_type || r.kind || '—')} />
              {r.simulated === true && <Badge text="SIMULATED" tone="warn" />}
            </header>
            <dl className="inv-card-meta">
              <dt>資料來源</dt><dd>{String(r.source || r.data_source || '—')}</dd>
              <dt>分析時間</dt><dd>{String(r.generated_at || r.at || '—')}</dd>
              <dt>版本</dt><dd>{String(r.version || '—')}</dd>
            </dl>
            <div className="inv-card-actions">
              <button onClick={() => setOpen(r)}>檢視</button>
              <button onClick={() => {
                const blob = new Blob([JSON.stringify(r, null, 2)],
                  { type: 'application/json' })
                const a = document.createElement('a')
                a.href = URL.createObjectURL(blob)
                a.download = `report-${r.report_id || i}.json`
                a.click()
                URL.revokeObjectURL(a.href)
              }}>匯出</button>
            </div>
            <DataFreshnessIndicator record={r} />
          </article>
        ))}
        {!rows.length && (
          <EmptyState detail="尚無報告資料——報告由投資引擎排程產生後鏡像送達。" />
        )}
      </div>

      {open && (
        <Section title={`報告內容：${String(open.title || '')}`}
          aside={<button onClick={() => setOpen(null)}>關閉</button>}>
          <pre className="inv-json">{JSON.stringify(open, null, 2)}</pre>
          <p className="inv-note">
            標記 SIMULATED 之數值為模擬結果，非真實投資獲利。
          </p>
        </Section>
      )}
    </div>
  )
}
