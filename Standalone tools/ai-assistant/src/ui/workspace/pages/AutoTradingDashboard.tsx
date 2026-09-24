import React, { useState } from 'react'
import { useCommandQuery, useIpcEvent } from '../hooks'
import { DataFreshnessIndicator, SimulatedBadge } from '../freshness'
import { DataTable, EmptyState, Metric, Pnl, Section, Badge } from '../common'
import { useNotify, useUIState } from '../uiState'

type Props = { sendCommand: (c: string, p?: unknown) => { ok: boolean; message?: string } }

const arr = (v: unknown): Record<string, unknown>[] =>
  Array.isArray(v) ? (v as Record<string, unknown>[]) : []
const num = (v: unknown): number => (Number.isFinite(Number(v)) ? Number(v) : 0)

export default function AutoTradingDashboard({ sendCommand }: Props) {
  const notify = useNotify()
  const { state } = useUIState()
  const [ctrlResult, setCtrlResult] = useState('')

  const overview = useCommandQuery<any>(sendCommand, 'investment_autotrade_overview')
  const strats = useCommandQuery<any>(sendCommand, 'investment_autotrade_strategies')
  const perf = useCommandQuery<any>(sendCommand, 'investment_autotrade_performance')
  const sim = useCommandQuery<any>(sendCommand, 'investment_sim_status')
  const execs = useCommandQuery<any>(sendCommand, 'investment_sim_executions')
  const mode = useCommandQuery<any>(sendCommand, 'investment_trade_mode')

  // react to control results + mirror events
  useIpcEvent('investment_autotrade_control_result', (p) => {
    const d = p as Record<string, unknown>
    setCtrlResult(String(d?.error_code || d?.message || '已送出'))
    if (d?.ok === false) notify(`控制未執行：${d.error_code}`, 'WARNING')
  })

  const control = (action: string, strategy_id = '') => {
    const ack = sendCommand('investment_autotrade_control', { action, strategy_id })
    if (!ack.ok) notify(`控制未送出：${ack.message}`, 'ERROR')
  }

  const ov = (overview.data || {}) as Record<string, any>
  const strategies = arr(strats.data?.strategies)

  return (
    <div className="inv-page">
      <header className="inv-page-head">
        <h2>自動操盤控制台</h2>
        <SimulatedBadge />
      </header>

      <div className="inv-metric-grid">
        <Metric label="運行模式"
          value={String(mode.data?.mode || ov.mode || 'ANALYSIS')}
          sub="LIVE 未啟用" />
        <Metric label="星澄模型"
          value={ov.model_available === false ? '不可用（降級安全）' : '可用'}
          tone={ov.model_available === false ? 'down' : 'up'} />
        <Metric label="行情資料"
          value={String(ov.data_state || ov.data_quality || '—')}
          sub="過期即阻擋下單" />
        <Metric label="運行策略"
          value={`${num(ov.running)} / ${num(ov.total ?? strategies.length)}`} />
        <Metric label="模擬帳戶數" value={num(sim.data?.accounts)} />
        <Metric label="SHADOW/PAPER" value="SHADOW / PAPER"
          sub={String(ov.disclaimer || 'LIVE stays phase-locked')} />
      </div>

      <Section title="策略運行狀態" aside={
        <div className="inv-actions">
          <button onClick={() => control('start')}>開始策略</button>
          <button onClick={() => control('pause')}>暫停策略</button>
          <button onClick={() => control('stop')}>停止策略</button>
          <button onClick={() => control('set_capital')}>設定模擬資金</button>
        </div>
      }>
        {ctrlResult && (
          <p className="inv-note inv-ctrl-result">控制結果：{ctrlResult}</p>
        )}
        <DataTable
          rows={strategies}
          columns={[
            { key: 'strategy_id', label: '策略' },
            { key: 'version', label: '版本' },
            { key: 'execution_mode', label: '模式',
              render: (r) => <Badge text={String(r.execution_mode || '—')} /> },
            { key: 'state', label: '狀態' },
            { key: 'market_scope', label: '市場' },
            { key: 'action', label: '操作',
              render: (r) => (
                <span className="inv-row-actions">
                  <button onClick={() => control('start', String(r.strategy_id))}>啟動</button>
                  <button onClick={() => control('pause', String(r.strategy_id))}>暫停</button>
                  <button onClick={() => control('stop', String(r.strategy_id))}>停止</button>
                </span>
              ) },
          ]}
          empty={<EmptyState detail="尚無策略鏡像資料。" />}
        />
        <p className="inv-note">
          控制經治理通道提交至投資引擎；通道未接通時顯示 CONTROL_CHANNEL_UNAVAILABLE，不做假成功。
        </p>
      </Section>

      <Section title="策略績效（模擬）">
        <div className="inv-cards">
          {arr(perf.data?.strategies || perf.data?.performance).map((p: any, i) => (
            <article key={i} className="inv-card">
              <b>{String(p.strategy_id)}</b>
              <dl className="inv-card-meta">
                <dt>總損益</dt><dd><Pnl value={p.total_pnl} /></dd>
                <dt>勝率</dt><dd>{num(p.win_rate).toFixed(1)}%</dd>
                <dt>最大回撤</dt><dd>{num(p.max_drawdown).toFixed(2)}%</dd>
                <dt>成交數</dt><dd>{num(p.fills)}</dd>
              </dl>
              <DataFreshnessIndicator record={p} />
            </article>
          ))}
          {!arr(perf.data?.strategies || perf.data?.performance).length && (
            <EmptyState detail="尚無模擬績效資料。" />
          )}
        </div>
      </Section>

      <Section title="模擬成交">
        <DataTable
          rows={arr(execs.data?.executions)}
          columns={[
            { key: 'strategy_id', label: '策略' },
            { key: 'instrument_id', label: '商品' },
            { key: 'side', label: '方向' },
            { key: 'quantity', label: '數量' },
            { key: 'price', label: '價格' },
            { key: 'status', label: '狀態' },
            { key: 'at', label: '時間' },
          ]}
          empty={<EmptyState detail="尚無模擬成交。" />}
        />
      </Section>
    </div>
  )
}
