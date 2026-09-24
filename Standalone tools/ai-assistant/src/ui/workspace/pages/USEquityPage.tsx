import React, { useMemo, useState } from 'react'
import { useCommandQuery } from '../hooks'
import { DataFreshnessIndicator } from '../freshness'
import { InvestmentChart } from '../chart'
import { DataTable, EmptyState, Metric, Money, Pnl, Section } from '../common'
import { useUIState } from '../uiState'

type Props = { sendCommand: (c: string, p?: unknown) => { ok: boolean; message?: string } }

const arr = (v: unknown): Record<string, unknown>[] =>
  Array.isArray(v) ? (v as Record<string, unknown>[]) : []
const num = (v: unknown): number => (Number.isFinite(Number(v)) ? Number(v) : 0)

export default function USEquityPage({ sendCommand }: Props) {
  const { state } = useUIState()
  const [query, setQuery] = useState('')
  const [iid, setIid] = useState('')

  const portfolio = useCommandQuery<any>(sendCommand, 'investment_us_portfolio')
  const recs = useCommandQuery<any>(sendCommand, 'investment_us_recommendations')
  const analysis = useCommandQuery<any>(sendCommand, 'investment_us_analysis')
  const status = useCommandQuery<any>(sendCommand, 'investment_market_status')
  const quote = useCommandQuery<any>(
    sendCommand, 'investment_market_quote',
    { instrument_id: iid }, [iid])
  const history = useCommandQuery<any>(
    sendCommand, 'investment_market_history',
    { instrument_id: iid, timeframe: '1d', limit: 500 }, [iid])

  const positions = arr(portfolio.data?.positions || portfolio.data?.holdings)
  const us = status.data?.us || status.data?.markets?.us || {}
  const candles = useMemo(
    () =>
      arr(history.data?.candles).map((c: any) => ({
        x: c.candle_start || c.date,
        open: num(c.open), high: num(c.high),
        low: num(c.low), close: num(c.close), volume: num(c.volume),
      })),
    [history.data],
  )

  return (
    <div className="inv-page">
      <header className="inv-page-head">
        <h2>美國股票</h2>
        <div className="inv-head-badges">
          <span className="freshness freshness-delayed"><b>帳戶來源</b><i>MANUAL / FILE_IMPORT</i></span>
          <span className="inv-badge inv-badge-info">
            美股時段：{String(us.session || us.status || '—')}（依正式日曆/DST）
          </span>
        </div>
      </header>

      <Section title="富邦美股複委託帳戶（離線）" aside={
        <DataFreshnessIndicator record={positions[0]} source="FILE_IMPORT" />
      }>
        <DataTable
          rows={positions}
          columns={[
            { key: 'instrument_id', label: '商品' },
            { key: 'name', label: '名稱' },
            { key: 'quantity', label: '數量' },
            { key: 'average_cost', label: '美元成本',
              render: (r) => <Money value={r.average_cost} currency="USD" /> },
            { key: 'market_value', label: '美元市值',
              render: (r) => <Money value={r.market_value} currency="USD" /> },
            { key: 'market_value_twd', label: '台幣換算',
              render: (r) => <Money value={r.market_value_twd} currency="TWD" /> },
            { key: 'unrealized_pnl', label: '美元損益',
              render: (r) => <Pnl value={r.unrealized_pnl} /> },
            { key: 'distributions', label: 'ETF 配息',
              render: (r) => <Money value={r.distributions} currency="USD" /> },
          ]}
          empty={<EmptyState detail="尚無美股持倉資料。" />}
        />
      </Section>

      <Section title="行情與時段">
        <div className="inv-metric-grid">
          <Metric label="市場狀態" value={String(us.status || us.session || '—')}
            sub="依 TradingCalendar（America/New_York，含 DST）" />
          <Metric label="盤前" value={us.pre ? '開放' : '未開放/未授權'} />
          <Metric label="盤後" value={us.post ? '開放' : '未開放/未授權'} />
        </div>
        <div className="inv-search">
          <input value={query} placeholder="輸入代碼（例：AAPL）"
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && setIid(query.trim())} />
          <button onClick={() => setIid(query.trim())}>查詢</button>
        </div>
        {iid ? (
          <div className="inv-chart-grid">
            <InvestmentChart kind="candles" title={`${iid} K 線`} candles={candles}
              empty="無 K 線歷史" />
          </div>
        ) : (
          <EmptyState detail="輸入代碼查詢行情。" />
        )}
      </Section>

      <Section title="星澄分析與建議">
        <div className="inv-cards">
          {arr(recs.data?.recommendations).map((r: any, i) => (
            <article key={i} className="inv-card">
              <b>{String(r.instrument_id)}</b>
              <span className="inv-badge inv-badge-info">
                {String(r.recommendation_type || '—')}
              </span>
              <p>{String(r.reasoning || '')}</p>
              <DataFreshnessIndicator record={r} />
            </article>
          ))}
          {!arr(recs.data?.recommendations).length && (
            <EmptyState detail="尚無美股建議。" />
          )}
        </div>
      </Section>
      <p className="inv-note">富邦複委託保持離線——無連線、無同步、無真實下單。</p>
    </div>
  )
}
