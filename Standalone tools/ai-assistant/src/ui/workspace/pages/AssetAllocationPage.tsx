import React from 'react'
import { useCommandQuery } from '../hooks'
import { DataFreshnessIndicator } from '../freshness'
import { InvestmentChart } from '../chart'
import { DataTable, EmptyState, Section } from '../common'

type Props = { sendCommand: (c: string, p?: unknown) => { ok: boolean; message?: string } }

const arr = (v: unknown): Record<string, unknown>[] =>
  Array.isArray(v) ? (v as Record<string, unknown>[]) : []
const num = (v: unknown): number => (Number.isFinite(Number(v)) ? Number(v) : 0)

const DIMS = [
  { key: 'by_market', label: '市場配置' },
  { key: 'by_instrument', label: '商品配置' },
  { key: 'by_sector', label: '產業配置' },
  { key: 'by_country', label: '國家配置' },
  { key: 'by_currency', label: '幣別配置' },
  { key: 'cash', label: '現金配置' },
]

export default function AssetAllocationPage({ sendCommand }: Props) {
  const alloc = useCommandQuery<any>(sendCommand, 'investment_assets_allocation')
  const exposure = useCommandQuery<any>(sendCommand, 'investment_assets_exposure')
  const rebalance = useCommandQuery<any>(
    sendCommand, 'investment_monitor_recommendations')

  const a = (alloc.data?.allocation || alloc.data || {}) as Record<string, any>
  const overlaps = arr(exposure.data?.overlaps || exposure.data?.shared_exposures)
  const candidates = arr(
    rebalance.data?.recommendations,
  ).filter((r) =>
    String(r.recommendation_type || '').toLowerCase().includes('realloc') ||
    String(r.category || '').toLowerCase().includes('rebal'),
  )

  return (
    <div className="inv-page">
      <header className="inv-page-head">
        <h2>資產配置中心</h2>
        <DataFreshnessIndicator record={a as any} />
      </header>

      <div className="inv-chart-grid">
        {DIMS.map((d) => {
          const rows = arr(a[d.key])
          return (
            <InvestmentChart key={d.key} kind="donut" title={d.label}
              slices={rows.map((r: any) => ({
                label: String(r.label || r.sector || r.country || r.currency || r.market || r.instrument_id || '—'),
                value: num(r.value ?? r.weight ?? r.market_value),
              }))}
              empty={`${d.label}資料不足`} />
          )
        })}
      </div>

      <Section title="目標 vs 目前">
        <DataTable
          rows={arr(a.target_vs_current)}
          columns={[
            { key: 'bucket', label: '配置項' },
            { key: 'target', label: '目標' },
            { key: 'current', label: '目前' },
            { key: 'deviation', label: '偏差' },
          ]}
          empty={<EmptyState kind="insufficient"
            detail="尚未設定目標配置——整合未完成。" />}
        />
      </Section>

      <Section title="再平衡候選方案">
        <div className="inv-cards">
          {candidates.map((r: any, i) => (
            <article key={i} className="inv-card">
              <b>{String(r.title || r.instrument_id || '再配置')}</b>
              <p>{String(r.reasoning || r.summary || '')}</p>
              <DataFreshnessIndicator record={r} />
            </article>
          ))}
          {!candidates.length && (
            <EmptyState detail="目前無再平衡建議。" />
          )}
        </div>
      </Section>

      <Section title="跨商品曝險（共同企業/產業辨識）">
        <DataTable
          rows={overlaps}
          columns={[
            { key: 'entity', label: '共同曝險標的' },
            { key: 'instruments', label: '涉及商品',
              render: (r) => arr(r.instruments).join(', ') || '—' },
            { key: 'combined_weight', label: '合併權重' },
            { key: 'combined_value', label: '合併市值' },
          ]}
          empty={<EmptyState kind="insufficient"
            detail="曝險穿透資料送達後顯示（例：台積電/TSM ADR/半導體 ETF/科技基金）。" />}
        />
        <p className="inv-note">
          ETF/基金穿透持股僅供曝險辨識，不重複加總至總資產。
        </p>
      </Section>
    </div>
  )
}
