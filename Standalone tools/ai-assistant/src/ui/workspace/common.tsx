import React, { useMemo, useState } from 'react'
import { fmt } from './chart'

/** Honest empty states (§一): a page/section that lacks backend support
 * says so — it never fabricates data. */
export function EmptyState(props: {
  kind?: 'no_data' | 'insufficient' | 'not_integrated'
  detail?: string
}) {
  const label =
    props.kind === 'not_integrated'
      ? '整合未完成'
      : props.kind === 'insufficient'
        ? '資料不足'
        : '尚未提供'
  return (
    <div className={`inv-empty inv-empty-${props.kind || 'no_data'}`}>
      <b>{label}</b>
      {props.detail && <p>{props.detail}</p>}
    </div>
  )
}

export function Section(props: {
  title: string
  aside?: React.ReactNode
  children: React.ReactNode
}) {
  return (
    <section className="inv-section">
      <header className="inv-section-head">
        <h3>{props.title}</h3>
        {props.aside}
      </header>
      {props.children}
    </section>
  )
}

export function Metric(props: {
  label: string
  value: React.ReactNode
  sub?: React.ReactNode
  tone?: 'up' | 'down' | 'flat'
}) {
  return (
    <div className={`inv-metric inv-metric-${props.tone || 'flat'}`}>
      <span className="inv-metric-label">{props.label}</span>
      <b className="inv-metric-value">{props.value}</b>
      {props.sub && <span className="inv-metric-sub">{props.sub}</span>}
    </div>
  )
}

export function Badge(props: { text: string; tone?: string }) {
  return <span className={`inv-badge inv-badge-${props.tone || 'info'}`}>{props.text}</span>
}

const num = (v: unknown): number => {
  const n = Number(v)
  return Number.isFinite(n) ? n : 0
}

export function Money(props: { value: unknown; currency?: string }) {
  const n = num(props.value)
  return (
    <span className="inv-money">
      {props.currency ? `${props.currency} ` : ''}
      {fmt(n)}
    </span>
  )
}

export function Pnl(props: { value: unknown }) {
  const n = num(props.value)
  return (
    <span className={n > 0 ? 'pnl-up' : n < 0 ? 'pnl-down' : ''}>
      {n > 0 ? '+' : ''}
      {fmt(n)}
    </span>
  )
}

/**
 * Windowed table — renders at most `pageSize` rows at a time with
 * pager controls, so large histories never dump every row into the DOM.
 */
export function DataTable(props: {
  columns: { key: string; label: string; render?: (row: any) => React.ReactNode }[]
  rows: Record<string, unknown>[]
  pageSize?: number
  empty?: React.ReactNode
}) {
  const size = props.pageSize ?? 25
  const [page, setPage] = useState(0)
  const rows = props.rows
  const pages = Math.max(1, Math.ceil(rows.length / size))
  const cur = Math.min(page, pages - 1)
  const slice = useMemo(
    () => rows.slice(cur * size, (cur + 1) * size),
    [rows, cur, size],
  )
  if (!rows.length) return <>{props.empty ?? <EmptyState />}</>
  return (
    <div className="inv-table-wrap">
      <table className="inv-table">
        <thead>
          <tr>
            {props.columns.map((c) => (
              <th key={c.key}>{c.label}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {slice.map((r, i) => (
            <tr key={i}>
              {props.columns.map((c) => (
                <td key={c.key}>
                  {c.render ? c.render(r) : String(r[c.key] ?? '—')}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
      {pages > 1 && (
        <div className="inv-pager">
          <button disabled={cur === 0} onClick={() => setPage(cur - 1)}>
            上一頁
          </button>
          <span>
            {cur + 1} / {pages}（共 {rows.length} 筆）
          </span>
          <button disabled={cur >= pages - 1} onClick={() => setPage(cur + 1)}>
            下一頁
          </button>
        </div>
      )}
    </div>
  )
}
