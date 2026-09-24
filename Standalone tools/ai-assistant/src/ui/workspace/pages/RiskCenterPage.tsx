import React, { useState } from 'react'
import { useCommandQuery, useIpcEvent } from '../hooks'
import { DataFreshnessIndicator } from '../freshness'
import { DataTable, EmptyState, Metric, Section, Badge } from '../common'
import { useNotify } from '../uiState'

type Props = { sendCommand: (c: string, p?: unknown) => { ok: boolean; message?: string } }

const arr = (v: unknown): Record<string, unknown>[] =>
  Array.isArray(v) ? (v as Record<string, unknown>[]) : []
const num = (v: unknown): number => (Number.isFinite(Number(v)) ? Number(v) : 0)

export default function RiskCenterPage({ sendCommand }: Props) {
  const notify = useNotify()
  const [confirm, setConfirm] = useState<string | null>(null)

  const risk = useCommandQuery<any>(sendCommand, 'investment_monitor_risk')
  const events = useCommandQuery<any>(sendCommand, 'investment_monitor_events')
  const ov = useCommandQuery<any>(sendCommand, 'investment_autotrade_overview')

  useIpcEvent('investment_autotrade_control_result', (p) => {
    const d = p as Record<string, unknown>
    if (d?.ok === false)
      notify(`操作未執行：${d.error_code || d.message}`, 'WARNING')
  })

  const control = (action: string) => {
    const ack = sendCommand('investment_autotrade_control', { action })
    if (!ack.ok) notify(`操作未送出：${ack.message}`, 'ERROR')
    setConfirm(null)
  }

  const r = (risk.data || {}) as Record<string, any>

  return (
    <div className="inv-page">
      <header className="inv-page-head">
        <h2>風險中心</h2>
        <DataFreshnessIndicator record={r as any} />
      </header>

      <div className="inv-metric-grid">
        <Metric label="持倉風險" value={String(r.position_risk ?? '—')} />
        <Metric label="策略風險" value={String(r.strategy_risk ?? '—')} />
        <Metric label="市場風險" value={String(r.market_risk ?? '—')} />
        <Metric label="資金風險" value={String(r.capital_risk ?? '—')} />
        <Metric label="集中度" value={num(r.concentration).toFixed(2)} />
        <Metric label="最大回撤" value={`${num(r.max_drawdown).toFixed(2)}%`} />
        <Metric label="系統異常" value={num(ov.data?.faults ?? r.faults)} />
      </div>

      <Section title="風控事件">
        <DataTable
          rows={arr(events.data?.events || r.events)}
          columns={[
            { key: 'triggered_at', label: '觸發時間',
              render: (x) => <DataFreshnessIndicator record={x} /> },
            { key: 'trigger', label: '觸發條件',
              render: (x) => String(x.trigger || x.reason || x.event_type || '—') },
            { key: 'strategy_id', label: '影響策略' },
            { key: 'account_id', label: '影響帳戶' },
            { key: 'outcome', label: '風控結果',
              render: (x) => String(x.outcome || x.decision || '—') },
            { key: 'status', label: '處理狀態',
              render: (x) => <Badge text={String(x.status || '—')} /> },
          ]}
          empty={<EmptyState detail="尚無風控事件。" />}
        />
      </Section>

      <Section title="處置操作">
        <p className="inv-note">
          以下三者為不同語意的操作，分別送出：
        </p>
        <div className="inv-actions">
          <button className="inv-warn"
            onClick={() => setConfirm('halt_new_orders')}>
            停止新增訂單
          </button>
          <button className="inv-warn"
            onClick={() => setConfirm('cancel_orders')}>
            取消模擬委託
          </button>
          <button className="inv-danger"
            onClick={() => setConfirm('liquidate_positions')}>
            清算模擬持倉
          </button>
          <button onClick={() => control('pause_strategy')}>暫停策略</button>
          <button onClick={() => control('stop_all')}>停止模擬操盤</button>
        </div>
        {confirm && (
          <div className="inv-confirm">
            <p>
              確認執行「{{
                halt_new_orders: '停止新增訂單',
                cancel_orders: '取消模擬委託',
                liquidate_positions: '清算模擬持倉',
              }[confirm]}」？此操作經治理通道送至投資引擎重新驗證。
            </p>
            <button className="inv-danger" onClick={() => control(confirm)}>
              確認
            </button>
            <button onClick={() => setConfirm(null)}>取消</button>
          </div>
        )}
      </Section>
    </div>
  )
}
