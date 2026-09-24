import React, { useState } from 'react'
import { useCommandQuery, useIpcEvent } from '../hooks'
import { DataFreshnessIndicator } from '../freshness'
import { EmptyState, Section, Badge } from '../common'
import { useNotify } from '../uiState'

type Props = { sendCommand: (c: string, p?: unknown) => { ok: boolean; message?: string } }

const arr = (v: unknown): Record<string, unknown>[] =>
  Array.isArray(v) ? (v as Record<string, unknown>[]) : []

const FILTERS = [
  { key: 'all', label: '全部' },
  { key: 'tw', label: '台股' },
  { key: 'us', label: '美股' },
  { key: 'etf', label: 'ETF' },
  { key: 'fund', label: '基金' },
  { key: 'realloc', label: '再配置' },
]

function marketOf(r: Record<string, unknown>): string {
  const m = String(r.market || r.market_scope || '').toLowerCase()
  if (m) return m
  const t = String(r.recommendation_type || '').toLowerCase()
  if (t.includes('realloc')) return 'realloc'
  return ''
}

export default function RecommendationPage({ sendCommand }: Props) {
  const [filter, setFilter] = useState('all')
  const [read, setRead] = useState<Set<string>>(new Set())
  const notify = useNotify()

  const mon = useCommandQuery<any>(sendCommand, 'investment_monitor_recommendations')
  const tw = useCommandQuery<any>(sendCommand, 'investment_tw_recommendations')
  const us = useCommandQuery<any>(sendCommand, 'investment_us_recommendations')
  const fund = useCommandQuery<any>(sendCommand, 'investment_fund_recommendations')

  const all: Record<string, unknown>[] = [
    ...arr(mon.data?.recommendations),
    ...arr(tw.data?.recommendations).map((r) => ({ ...r, market: 'tw' })),
    ...arr(us.data?.recommendations).map((r) => ({ ...r, market: 'us' })),
    ...arr(fund.data?.recommendations).map((r) => ({ ...r, market: 'fund' })),
  ]
  const rows = all.filter((r) => {
    if (filter === 'all') return true
    const m = marketOf(r)
    if (filter === 'etf') return String(r.asset_class || '').toLowerCase() === 'etf'
    return m === filter
  })

  const submitOnly = (label: string, action: string, r: any) => {
    // § governance: ai-assistant cannot command investment-mobile.
    // Send through the honest control command — it will return
    // CONTROL_CHANNEL_UNAVAILABLE which we surface to the user.
    const ack = sendCommand('investment_autotrade_control', {
      action, strategy_id: r.strategy_id || '',
      instrument_id: r.instrument_id || r.fund_id || '',
    })
    if (!ack.ok) notify(`${label}未送出：${ack.message}`, 'ERROR')
  }

  return (
    <div className="inv-page">
      <header className="inv-page-head">
        <h2>AI 買賣建議</h2>
        <div className="inv-filter">
          {FILTERS.map((f) => (
            <button key={f.key}
              className={filter === f.key ? 'active' : ''}
              onClick={() => setFilter(f.key)}>
              {f.label}
            </button>
          ))}
        </div>
      </header>

      <div className="inv-cards">
        {rows.map((r, i) => {
          const id = String(r.id || r.recommendation_id || i)
          const isRead = read.has(id) || r.status === 'READ'
          return (
            <article key={id} className={`inv-card${isRead ? ' read' : ''}`}>
              <header className="inv-card-head">
                <b>{String(r.instrument_id || r.fund_id || '—')}</b>
                <Badge text={String(r.recommendation_type || r.action || '—')} />
                {isRead && <Badge text="已讀" tone="muted" />}
              </header>
              <p className="inv-card-reason">{String(r.reasoning || '—')}</p>
              <dl className="inv-card-meta">
                <dt>風險因素</dt>
                <dd>{String(r.risk_factors || r.risks || '—')}</dd>
                <dt>資料日期</dt>
                <dd>{String(r.data_date || r.generated_at || '—')}</dd>
                <dt>模型版本</dt>
                <dd>{String(r.model_version || '—')}</dd>
                <dt>策略版本</dt>
                <dd>{String(r.strategy_version || '—')}</dd>
                <dt>有效期間</dt>
                <dd>{String(r.valid_until || r.expires_at || '—')}</dd>
              </dl>
              <DataFreshnessIndicator record={r} />
              <div className="inv-card-actions">
                <button onClick={() =>
                  setRead((s) => new Set(s).add(id))}>標記已閱讀</button>
                <button onClick={() =>
                  submitOnly('SHADOW 研究', 'start_shadow_research', r)
                }>加入 SHADOW 研究</button>
                <button onClick={() =>
                  submitOnly('PAPER 方案', 'create_paper_plan', r)
                }>建立 PAPER 模擬方案</button>
              </div>
            </article>
          )
        })}
        {!rows.length && (
          <EmptyState detail="目前無符合篩選的建議。" />
        )}
      </div>
      <p className="inv-note">
        本階段不提供真實下單；SHADOW/PAPER 操作經治理通道提交，通道未接通時會明確提示。
      </p>
    </div>
  )
}
