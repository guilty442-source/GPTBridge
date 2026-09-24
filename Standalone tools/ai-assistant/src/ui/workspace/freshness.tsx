import React from 'react'

/** Unified data-state taxonomy (§18). */
export type Freshness =
  | 'CURRENT'
  | 'DELAYED'
  | 'STALE'
  | 'INCOMPLETE'
  | 'UNAVAILABLE'
  | 'SIMULATED'

const LABELS: Record<Freshness, string> = {
  CURRENT: '即時',
  DELAYED: '延遲',
  STALE: '已過期',
  INCOMPLETE: '不完整',
  UNAVAILABLE: '無資料',
  SIMULATED: '模擬',
}

const SOURCE_LABELS: Record<string, string> = {
  MANUAL: '手動輸入',
  FILE_IMPORT: '檔案匯入',
  PAPER: '模擬交易',
  SHADOW: '影子訊號',
  BACKTEST: '回測',
  AI_ANALYSIS: '模型分析',
  CONFIRMED: '已確認行情',
  SIMULATED: '模擬資料',
}

export function freshnessLabel(f: Freshness): string {
  return LABELS[f]
}

/** Derive a freshness class from common record fields. Never invents
 * timestamps — missing data degrades to UNAVAILABLE/INCOMPLETE. */
export function freshnessOf(rec: Record<string, unknown> | undefined | null): {
  freshness: Freshness
  at: number | null
} {
  if (!rec) return { freshness: 'UNAVAILABLE', at: null }
  if (rec.simulated === true) return { freshness: 'SIMULATED', at: tsOf(rec) }
  const at = tsOf(rec)
  if (at === null) return { freshness: 'INCOMPLETE', at: null }
  const age = Date.now() - at
  const staleAfter = Number(rec.stale_after_s || 0) * 1000 || 3 * 86_400_000
  if (age > staleAfter) return { freshness: 'STALE', at }
  if (age > 60_000) return { freshness: 'DELAYED', at }
  return { freshness: 'CURRENT', at }
}

function tsOf(rec: Record<string, unknown>): number | null {
  for (const k of [
    'at',
    'timestamp',
    'source_timestamp',
    'detected_at',
    'recorded_at',
    'generated_at',
    'market_data_timestamp',
    'updated_at',
    'created_at',
    'nav_date',
    'candle_end',
  ]) {
    const v = rec[k]
    if (v === undefined || v === null || v === '') continue
    const n =
      typeof v === 'number'
        ? v < 1e12
          ? v * 1000
          : v
        : Date.parse(String(v))
    if (!Number.isNaN(n)) return n
  }
  return null
}

export function formatTime(at: number | null): string {
  if (at === null) return '—'
  return new Date(at).toLocaleString('zh-TW', { hour12: false })
}

export function DataFreshnessIndicator(props: {
  record?: Record<string, unknown> | null
  source?: string
  extra?: string
}) {
  const { freshness, at } = freshnessOf(props.record ?? undefined)
  const src = props.source || String(props.record?.source_id || props.record?.data_source || '')
  return (
    <span className={`freshness freshness-${freshness.toLowerCase()}`}>
      <b>{freshnessLabel(freshness)}</b>
      {src && <i>{SOURCE_LABELS[src] || src}</i>}
      <time>{formatTime(at)}</time>
      {props.extra && <i>{props.extra}</i>}
    </span>
  )
}

export function SimulatedBadge() {
  return <span className="freshness freshness-simulated"><b>SIMULATED</b><i>模擬資料</i></span>
}
