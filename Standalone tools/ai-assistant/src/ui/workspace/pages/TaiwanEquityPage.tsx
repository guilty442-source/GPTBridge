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

export default function TaiwanEquityPage({ sendCommand }: Props) {
  const { state } = useUIState()
  const [query, setQuery] = useState('')
  const [iid, setIid] = useState(state.instrumentId || '')

  const portfolio = useCommandQuery<any>(sendCommand, 'investment_tw_portfolio')
  const recs = useCommandQuery<any>(sendCommand, 'investment_tw_recommendations')
  const analysis = useCommandQuery<any>(sendCommand, 'investment_tw_analysis')
  const status = useCommandQuery<any>(sendCommand, 'investment_market_status')
  const quote = useCommandQuery<any>(
    sendCommand, 'investment_market_quote',
    { instrument_id: iid }, [iid])
  const history = useCommandQuery<any>(
    sendCommand, 'investment_market_history',
    { instrument_id: iid, timeframe: '1d', limit: 500 }, [iid])

  const positions = arr(portfolio.data?.positions || portfolio.data?.holdings)
  const candles = useMemo(
    () =>
      arr(history.data?.candles).map((c: any) => ({
        x: c.candle_start || c.date,
        open: num(c.open), high: num(c.high),
        low: num(c.low), close: num(c.close), volume: num(c.volume),
      })),
    [history.data],
  )
  const trend = useMemo(
    () => candles.map((c) => ({ x: c.x, y: c.close })),
    [candles],
  )

  return (
    <div className="inv-page">
      <header className="inv-page-head">
        <h2>台灣股票</h2>
        <div className="inv-head-badges">
          <span className="freshness freshness-delayed"><b>帳戶來源</b><i>MANUAL / FILE_IMPORT / PAPER</i></span>
          <DataFreshnessIndicator record={status.data as any} />
        </div>
      </header>

      <Section title="國泰台股帳戶（離線）" aside={
        <DataFreshnessIndicator record={positions[0]} source="FILE_IMPORT" />
      }>
        <DataTable
          rows={positions}
          columns={[
            { key: 'instrument_id', label: '商品' },
            { key: 'name', label: '名稱' },
            { key: 'quantity', label: '數量' },
            { key: 'average_cost', label: '平均成本',
              render: (r) => <Money value={r.average_cost} /> },
            { key: 'market_value', label: '參考市值',
              render: (r) => <Money value={r.market_value} /> },
            { key: 'unrealized_pnl', label: '未實現損益',
              render: (r) => <Pnl value={r.unrealized_pnl} /> },
            { key: 'dividends', label: '累計股息',
              render: (r) => <Money value={r.dividends} /> },
            { key: 'source', label: '來源',
              render: (r) => <span className="inv-src">{String(r.source || 'MANUAL')}</span> },
          ]}
          empty={<EmptyState detail="尚無台股持倉資料——請由資料匯入中心匯入。" />}
        />
      </Section>

      <Section title="行情">
        <div className="inv-search">
          <input
            value={query}
            placeholder="輸入股票代碼（例：2330）"
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && setIid(query.trim())}
          />
          <button onClick={() => setIid(query.trim())}>查詢</button>
        </div>
        {iid ? (
          <>
            <div className="inv-metric-grid">
              <Metric label="最新價" value={num(quote.data?.quote?.price ?? quote.data?.quote?.close) || '—'}
                sub={<DataFreshnessIndicator record={quote.data?.quote as any} />} />
              <Metric label="成交量" value={num(quote.data?.quote?.volume) || '—'} />
            </div>
            <div className="inv-chart-grid">
              <InvestmentChart kind="candles" title={`${iid} K 線`} candles={candles}
                empty="無 K 線歷史" />
              <InvestmentChart kind="line" title="收盤趨勢" points={trend}
                empty="無歷史" />
            </div>
          </>
        ) : (
          <EmptyState detail="輸入代碼查詢行情。" />
        )}
      </Section>

      <Section title="星澄分析與建議">
        <div className="inv-cards">
          {arr(recs.data?.recommendations).map((r: any, i) => (
            <article key={i} className="inv-card">
              <b>{String(r.instrument_id)}</b>
              <span className={`inv-badge inv-badge-${String(r.recommendation_type || '').toLowerCase()}`}>
                {String(r.recommendation_type || '—')}
              </span>
              <p>{String(r.reasoning || '')}</p>
              <DataFreshnessIndicator record={r} />
            </article>
          ))}
          {!arr(recs.data?.recommendations).length && (
            <EmptyState detail="尚無台股建議。" />
          )}
        </div>
        {analysis.data ? (
          <pre className="inv-json">{JSON.stringify(analysis.data, null, 2)}</pre>
        ) : (
          <EmptyState kind="insufficient" detail="分析結果尚未產生。" />
        )}
      </Section>
    </div>
  )
}
