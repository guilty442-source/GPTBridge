import React, { useMemo, useState } from 'react'
import { useCommandQuery } from '../hooks'
import { DataFreshnessIndicator } from '../freshness'
import { InvestmentChart } from '../chart'
import { DataTable, EmptyState, Metric, Money, Pnl, Section, Badge } from '../common'

type Props = { sendCommand: (c: string, p?: unknown) => { ok: boolean; message?: string } }

const arr = (v: unknown): Record<string, unknown>[] =>
  Array.isArray(v) ? (v as Record<string, unknown>[]) : []
const num = (v: unknown): number => (Number.isFinite(Number(v)) ? Number(v) : 0)

const FUND_ACTIONS: Record<string, string> = {
  SUBSCRIBE: '申購', ADD: '加碼', HOLD: '持有',
  REDUCE: '減碼', REDEEM: '贖回', SWITCH: '轉換',
}

export default function MutualFundPage({ sendCommand }: Props) {
  const [fundId, setFundId] = useState('')
  const [sc, setSc] = useState('A')

  const portfolio = useCommandQuery<any>(sendCommand, 'investment_fund_portfolio')
  const nav = useCommandQuery<any>(
    sendCommand, 'investment_fund_nav',
    { fund_id: fundId, share_class_id: sc }, [fundId, sc])
  const recs = useCommandQuery<any>(sendCommand, 'investment_fund_recommendations')
  const txns = useCommandQuery<any>(sendCommand, 'investment_fund_transactions')
  const analysis = useCommandQuery<any>(
    sendCommand, 'investment_fund_analysis',
    { fund_id: fundId }, [fundId])

  const funds = arr(portfolio.data?.funds || portfolio.data?.positions)
  const navHist = useMemo(
    () =>
      arr(nav.data?.navs || nav.data?.history).map((n: any) => ({
        x: n.nav_date, y: num(n.nav),
      })),
    [nav.data],
  )
  const latestNav = nav.data?.latest || nav.data?.nav

  return (
    <div className="inv-page">
      <header className="inv-page-head">
        <h2>共同基金</h2>
        <span className="inv-badge inv-badge-info">已公告 NAV——非即時成交價</span>
      </header>

      <Section title="基金清單" aside={
        <DataFreshnessIndicator record={funds[0]} source="FILE_IMPORT" />
      }>
        <DataTable
          rows={funds}
          columns={[
            { key: 'fund_id', label: '基金' },
            { key: 'name', label: '名稱' },
            { key: 'isin', label: 'ISIN' },
            { key: 'share_class_id', label: '級別' },
            { key: 'currency', label: '幣別' },
            { key: 'units', label: '持有單位' },
            { key: 'average_cost', label: '平均成本',
              render: (r) => <Money value={r.average_cost} /> },
            { key: 'nav', label: '最新 NAV',
              render: (r) => <Money value={r.nav} /> },
            { key: 'nav_date', label: 'NAV 日期' },
            { key: 'market_value', label: '參考市值',
              render: (r) => <Money value={r.market_value} /> },
            { key: 'total_return', label: '含息損益',
              render: (r) => <Pnl value={r.total_return} /> },
          ]}
          empty={<EmptyState detail="尚無基金持倉——請由資料匯入中心匯入。" />}
        />
      </Section>

      <Section title="基金詳細資訊">
        <div className="inv-search">
          <input value={fundId} placeholder="基金代碼"
            onChange={(e) => setFundId(e.target.value)} />
          <input value={sc} placeholder="級別" style={{ width: 64 }}
            onChange={(e) => setSc(e.target.value)} />
        </div>
        {fundId ? (
          <>
            <div className="inv-metric-grid">
              <Metric label="最新已公告 NAV"
                value={<Money value={latestNav?.nav} />}
                sub={`NAV 日期 ${latestNav?.nav_date || '—'}`} />
              <Metric label="NAV 狀態"
                value={nav.data?.stale ? '已過期' : '有效'}
                tone={nav.data?.stale ? 'down' : 'up'} />
            </div>
            <div className="inv-chart-grid">
              <InvestmentChart kind="line" title="歷史淨值" points={navHist}
                empty="無淨值歷史" />
            </div>
            <div className="inv-cards">
              {['績效', '配息', '費用', '持股', '產業配置', '國家配置', '公告'].map((k) => (
                <article key={k} className="inv-card">
                  <b>{k}</b>
                  <DataFreshnessIndicator record={(nav.data?.detail || {})[k] as any} />
                  <p>{JSON.stringify((nav.data?.detail || {})[k] ?? '尚未提供')}</p>
                </article>
              ))}
            </div>
          </>
        ) : (
          <EmptyState detail="輸入基金代碼檢視詳細資訊。" />
        )}
      </Section>

      <Section title="星澄基金建議">
        <div className="inv-cards">
          {arr(recs.data?.recommendations).map((r: any, i) => (
            <article key={i} className="inv-card">
              <b>{String(r.instrument_id || r.fund_id)}</b>
              <Badge text={FUND_ACTIONS[String(r.recommendation_type)] ||
                String(r.recommendation_type || '—')} />
              <p>{String(r.reasoning || '')}</p>
              <DataFreshnessIndicator record={r} />
            </article>
          ))}
          {!arr(recs.data?.recommendations).length && (
            <EmptyState detail="尚無基金建議。" />
          )}
        </div>
      </Section>

      <Section title="基金交易紀錄">
        <DataTable
          rows={arr(txns.data?.transactions)}
          columns={[
            { key: 'fund_id', label: '基金' },
            { key: 'type', label: '類型' },
            { key: 'units', label: '單位' },
            { key: 'amount', label: '金額', render: (r) => <Money value={r.amount} /> },
            { key: 'nav_date', label: 'NAV 日期' },
            { key: 'at', label: '時間' },
          ]}
          empty={<EmptyState detail="尚無基金交易紀錄。" />}
        />
      </Section>
      <p className="inv-note">
        基金申購/贖回不適用股票即時成交流程——本頁面不提供即時下單。
      </p>
    </div>
  )
}
