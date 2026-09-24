import React, { useMemo } from 'react'

export type ChartPoint = { x: number | string; y: number }
export type Candle = {
  x: number | string
  open: number
  high: number
  low: number
  close: number
  volume?: number
}

/** Bucket-average downsampling — the renderer never receives the full
 * tick history; at most `max` points reach the DOM. */
export function downsample<T extends { y: number }>(rows: T[], max = 240): T[] {
  if (rows.length <= max) return rows
  const out: T[] = []
  const bucket = rows.length / max
  for (let i = 0; i < max; i++) {
    const slice = rows.slice(Math.floor(i * bucket), Math.floor((i + 1) * bucket))
    if (!slice.length) continue
    const y = slice.reduce((s, r) => s + r.y, 0) / slice.length
    out.push({ ...slice[slice.length - 1], y })
  }
  return out
}

export function downsampleCandles(rows: Candle[], max = 160): Candle[] {
  if (rows.length <= max) return rows
  const out: Candle[] = []
  const bucket = rows.length / max
  for (let i = 0; i < max; i++) {
    const slice = rows.slice(Math.floor(i * bucket), Math.floor((i + 1) * bucket))
    if (!slice.length) continue
    out.push({
      x: slice[slice.length - 1].x,
      open: slice[0].open,
      high: Math.max(...slice.map((c) => c.high)),
      low: Math.min(...slice.map((c) => c.low)),
      close: slice[slice.length - 1].close,
      volume: slice.reduce((s, c) => s + (c.volume || 0), 0),
    })
  }
  return out
}

type ChartProps =
  | { kind: 'line' | 'area'; points: ChartPoint[]; height?: number }
  | { kind: 'donut'; slices: { label: string; value: number }[]; height?: number }
  | { kind: 'bars'; bars: { label: string; value: number }[]; height?: number }
  | { kind: 'candles'; candles: Candle[]; height?: number }

/** Shared SVG chart — memoized so quote ticks don't repaint the page. */
export const InvestmentChart = React.memo(function InvestmentChart(
  props: ChartProps & { title?: string; empty?: string },
) {
  const h = props.height ?? 180
  const body = useMemo(() => renderChart(props, h), [JSON.stringify(props)])
  return (
    <figure className="inv-chart">
      {props.title && <figcaption>{props.title}</figcaption>}
      {body ?? <div className="inv-empty">{props.empty || '資料不足'}</div>}
    </figure>
  )
})

const W = 640

function renderChart(props: ChartProps, h: number): React.ReactNode | null {
  if (props.kind === 'line' || props.kind === 'area') {
    const pts = downsample(props.points.filter((p) => Number.isFinite(p.y)))
    if (pts.length < 2) return null
    const ys = pts.map((p) => p.y)
    const lo = Math.min(...ys)
    const hi = Math.max(...ys)
    const span = hi - lo || 1
    const step = W / (pts.length - 1)
    const xy = pts.map(
      (p, i) => `${(i * step).toFixed(1)},${(h - 14 - ((p.y - lo) / span) * (h - 28)).toFixed(1)}`,
    )
    const path = `M${xy.join(' L')}`
    return (
      <svg viewBox={`0 0 ${W} ${h}`} preserveAspectRatio="none" className="inv-svg">
        {props.kind === 'area' && (
          <path
            d={`${path} L${W},${h - 14} L0,${h - 14} Z`}
            className="inv-chart-area"
          />
        )}
        <path d={path} className="inv-chart-line" fill="none" />
        <text x={4} y={14} className="inv-chart-tick">{fmt(hi)}</text>
        <text x={4} y={h - 4} className="inv-chart-tick">{fmt(lo)}</text>
      </svg>
    )
  }
  if (props.kind === 'candles') {
    const cs = downsampleCandles(props.candles)
    if (cs.length < 2) return null
    const lo = Math.min(...cs.map((c) => c.low))
    const hi = Math.max(...cs.map((c) => c.high))
    const span = hi - lo || 1
    const bw = Math.max(1, (W / cs.length) * 0.6)
    const Y = (v: number) => h - 14 - ((v - lo) / span) * (h - 28)
    return (
      <svg viewBox={`0 0 ${W} ${h}`} preserveAspectRatio="none" className="inv-svg">
        {cs.map((c, i) => {
          const x = (i + 0.5) * (W / cs.length)
          const up = c.close >= c.open
          return (
            <g key={i} className={up ? 'c-up' : 'c-down'}>
              <line x1={x} x2={x} y1={Y(c.high)} y2={Y(c.low)} strokeWidth={1} />
              <rect
                x={x - bw / 2}
                y={Math.min(Y(c.open), Y(c.close))}
                width={bw}
                height={Math.max(1, Math.abs(Y(c.open) - Y(c.close)))}
              />
            </g>
          )
        })}
      </svg>
    )
  }
  if (props.kind === 'donut') {
    const total = props.slices.reduce((s, x) => s + Math.max(0, x.value), 0)
    if (total <= 0) return null
    const r = h / 2 - 12
    const cx = 100
    const cy = h / 2
    let angle = -Math.PI / 2
    const arcs = props.slices
      .filter((s) => s.value > 0)
      .map((s, i) => {
        const a = (s.value / total) * Math.PI * 2
        const x1 = cx + r * Math.cos(angle)
        const y1 = cy + r * Math.sin(angle)
        angle += a
        const x2 = cx + r * Math.cos(angle)
        const y2 = cy + r * Math.sin(angle)
        const large = a > Math.PI ? 1 : 0
        return (
          <path
            key={i}
            d={`M${cx},${cy} L${x1},${y1} A${r},${r} 0 ${large} 1 ${x2},${y2} Z`}
            className={`donut-slice donut-${i % 8}`}
          />
        )
      })
    return (
      <div className="inv-donut-wrap">
        <svg viewBox={`0 0 200 ${h}`} className="inv-svg inv-donut">
          {arcs}
          <circle cx={cx} cy={cy} r={r * 0.55} className="donut-hole" />
        </svg>
        <ul className="inv-donut-legend">
          {props.slices.map((s, i) => (
            <li key={i}>
              <i className={`dot donut-${i % 8}`} />
              {s.label} — {((s.value / total) * 100).toFixed(1)}%
            </li>
          ))}
        </ul>
      </div>
    )
  }
  if (props.kind !== 'bars') return null
  const max = Math.max(...props.bars.map((b) => Math.abs(b.value)), 0) || 1
  if (!props.bars.length) return null
  return (
    <div className="inv-bars" style={{ minHeight: h }}>
      {props.bars.map((b: { label: string; value: number }, i: number) => (
        <div key={i} className="inv-bar-row">
          <span className="inv-bar-label">{b.label}</span>
          <div className="inv-bar-track">
            <div
              className={`inv-bar-fill${b.value < 0 ? ' neg' : ''}`}
              style={{ width: `${Math.min(100, (Math.abs(b.value) / max) * 100)}%` }}
            />
          </div>
          <span className="inv-bar-val">{fmt(b.value)}</span>
        </div>
      ))}
    </div>
  )
}

export function fmt(v: number): string {
  if (!Number.isFinite(v)) return '—'
  const a = Math.abs(v)
  if (a >= 1e8) return `${(v / 1e8).toFixed(2)}億`
  if (a >= 1e4) return `${(v / 1e4).toFixed(1)}萬`
  return v.toLocaleString('zh-TW', { maximumFractionDigits: 2 })
}
