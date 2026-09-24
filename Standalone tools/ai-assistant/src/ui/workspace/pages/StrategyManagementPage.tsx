import React, { useState } from 'react'
import { useCommandQuery, useIpcEvent } from '../hooks'
import { DataFreshnessIndicator } from '../freshness'
import { DataTable, EmptyState, Section, Badge } from '../common'
import { useNotify } from '../uiState'

type Props = { sendCommand: (c: string, p?: unknown) => { ok: boolean; message?: string } }

const arr = (v: unknown): Record<string, unknown>[] =>
  Array.isArray(v) ? (v as Record<string, unknown>[]) : []
const num = (v: unknown): number => (Number.isFinite(Number(v)) ? Number(v) : 0)

export default function StrategyManagementPage({ sendCommand }: Props) {
  const notify = useNotify()
  const [detail, setDetail] = useState<Record<string, unknown> | null>(null)

  const strats = useCommandQuery<any>(sendCommand, 'investment_autotrade_strategies')
  const defs = useCommandQuery<any>(sendCommand, 'investment_strategy_list')
  const perf = useCommandQuery<any>(sendCommand, 'investment_autotrade_performance')

  useIpcEvent('investment_autotrade_control_result', (p) => {
    const d = p as Record<string, unknown>
    notify(
      d?.ok === false
        ? `操作未執行：${d.error_code || d.message}`
        : `操作已受理`,
      d?.ok === false ? 'WARNING' : 'INFO',
    )
  })

  const control = (action: string, strategy_id = '', extra: object = {}) => {
    const ack = sendCommand('investment_autotrade_control', {
      action, strategy_id, ...extra,
    })
    if (!ack.ok) notify(`操作未送出：${ack.message}`, 'ERROR')
  }

  const runtimes = arr(strats.data?.strategies)
  const perfById = Object.fromEntries(
    arr(perf.data?.strategies || perf.data?.performance).map(
      (p: any) => [String(p.strategy_id), p],
    ),
  )

  return (
    <div className="inv-page">
      <header className="inv-page-head">
        <h2>多策略管理</h2>
        <div className="inv-actions">
          <button onClick={() => control('create_draft')}>新增策略草稿</button>
        </div>
      </header>

      <Section title="策略運行">
        <DataTable
          rows={runtimes}
          columns={[
            { key: 'strategy_id', label: '名稱' },
            { key: 'version', label: '版本' },
            { key: 'market_scope', label: '適用市場' },
            { key: 'instrument_scope', label: '監測商品',
              render: (r) => arr(r.instrument_scope).join(', ') || '—' },
            { key: 'execution_mode', label: '運行模式',
              render: (r) => <Badge text={String(r.execution_mode || '—')} /> },
            { key: 'capital', label: '分配資金' },
            { key: 'capital_used', label: '資金使用率' },
            { key: 'state', label: '運行狀態',
              render: (r) => <Badge text={String(r.state || '—')}
                tone={r.state === 'RUNNING' ? 'ok' : r.state === 'RISK_HALTED' ? 'danger' : 'info'} /> },
            { key: 'pnl', label: '策略損益',
              render: (r) => {
                const p = perfById[String(r.strategy_id)]
                return p ? <span>{num(p.total_pnl).toFixed(2)}</span> : '—'
              } },
            { key: 'dd', label: '最大回撤',
              render: (r) => {
                const p = perfById[String(r.strategy_id)]
                return p ? `${num(p.max_drawdown).toFixed(2)}%` : '—'
              } },
            { key: 'ops', label: '操作',
              render: (r) => (
                <span className="inv-row-actions">
                  <button onClick={() => setDetail(r)}>檢視</button>
                  <button onClick={() => control('start', String(r.strategy_id))}>啟動模擬</button>
                  <button onClick={() => control('pause', String(r.strategy_id))}>暫停</button>
                  <button onClick={() => control('stop', String(r.strategy_id))}>停止</button>
                </span>
              ) },
          ]}
          empty={<EmptyState detail="尚無策略運行資料。" />}
        />
      </Section>

      {detail && (
        <Section title={`策略詳細：${String(detail.strategy_id)}`}
          aside={<button onClick={() => setDetail(null)}>關閉</button>}>
          <pre className="inv-json">{JSON.stringify(detail, null, 2)}</pre>
          <p className="inv-note">
            RUNNING 策略的正式版本不可由此修改——修改請建立新版本草稿（草案參數僅在 DRAFT 可編輯）。
          </p>
          <button onClick={() =>
            control('edit_draft', String(detail.strategy_id))
          }>修改草稿參數（建立新版本）</button>
        </Section>
      )}

      <Section title="策略定義（鏡像）">
        <DataTable
          rows={arr(defs.data?.strategies)}
          columns={[
            { key: 'strategy_id', label: '策略' },
            { key: 'strategy_type', label: '型別' },
            { key: 'version', label: '版本' },
            { key: 'status', label: '狀態' },
            { key: 'market_scope', label: '市場' },
            { key: 'created_at', label: '建立時間' },
          ]}
          empty={<EmptyState detail="尚無策略定義鏡像。" />}
        />
      </Section>
    </div>
  )
}
