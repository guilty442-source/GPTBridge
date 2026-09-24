import React, { useMemo, useState } from 'react'
import { useCommandQuery, useIpcEvent } from '../hooks'
import { DataFreshnessIndicator } from '../freshness'
import { InvestmentChart } from '../chart'
import { DataTable, EmptyState, Metric, Pnl, Section, Badge } from '../common'
import { useNotify } from '../uiState'

type Props = { sendCommand: (c: string, p?: unknown) => { ok: boolean; message?: string } }

const arr = (v: unknown): Record<string, unknown>[] =>
  Array.isArray(v) ? (v as Record<string, unknown>[]) : []
const num = (v: unknown): number => (Number.isFinite(Number(v)) ? Number(v) : 0)

export default function StrategyLaboratoryPage({ sendCommand }: Props) {
  const notify = useNotify()
  const [sel, setSel] = useState<Record<string, unknown> | null>(null)
  const [form, setForm] = useState({
    strategy_id: '', instrument_id: '', from: '', to: '',
    initial_capital: '100000', fee: '', slippage: '',
  })

  const strats = useCommandQuery<any>(sendCommand, 'investment_strategy_list')
  const results = useCommandQuery<any>(sendCommand, 'investment_backtest_results')

  useIpcEvent('investment_autotrade_control_result', (p) => {
    const d = p as Record<string, unknown>
    if (d?.ok === false)
      notify(`回測未執行：${d.error_code || d.message}`, 'WARNING')
  })

  const rows = arr(results.data?.results)
  const detail = sel || rows[0]
  const curve = useMemo(
    () =>
      arr((detail as any)?.equity_curve || (detail as any)?.curve).map(
        (p: any, i) => ({ x: p.date || i, y: num(p.value ?? p.equity) }),
      ),
    [detail],
  )

  const launch = () => {
    const ack = sendCommand('investment_autotrade_control', {
      action: 'run_backtest',
      ...form,
      initial_capital: Number(form.initial_capital) || 0,
    })
    if (!ack.ok) notify(`回測未送出：${ack.message}`, 'ERROR')
  }

  return (
    <div className="inv-page">
      <header className="inv-page-head">
        <h2>策略實驗室</h2>
        <Badge text="回測/驗證結果" />
      </header>

      <Section title="回測設定">
        <div className="inv-form">
          <label>策略
            <select value={form.strategy_id}
              onChange={(e) => setForm({ ...form, strategy_id: e.target.value })}>
              <option value="">選擇策略</option>
              {arr(strats.data?.strategies).map((s: any, i) => (
                <option key={i} value={String(s.strategy_id)}>
                  {String(s.strategy_id)} v{String(s.version ?? '')}
                </option>
              ))}
            </select>
          </label>
          <label>商品
            <input value={form.instrument_id}
              onChange={(e) => setForm({ ...form, instrument_id: e.target.value })} />
          </label>
          <label>起始日
            <input type="date" value={form.from}
              onChange={(e) => setForm({ ...form, from: e.target.value })} />
          </label>
          <label>結束日
            <input type="date" value={form.to}
              onChange={(e) => setForm({ ...form, to: e.target.value })} />
          </label>
          <label>初始資金
            <input value={form.initial_capital}
              onChange={(e) => setForm({ ...form, initial_capital: e.target.value })} />
          </label>
          <label>交易費用
            <input value={form.fee} placeholder="預設費率"
              onChange={(e) => setForm({ ...form, fee: e.target.value })} />
          </label>
          <label>滑價假設
            <input value={form.slippage} placeholder="預設"
              onChange={(e) => setForm({ ...form, slippage: e.target.value })} />
          </label>
          <button className="inv-primary" onClick={launch}>啟動回測</button>
        </div>
        <p className="inv-note">
          回測於投資引擎端執行——此介面送出治理請求；通道未接通時顯示錯誤，不假造結果。
        </p>
      </Section>

      <Section title="回測結果">
        <DataTable
          rows={rows}
          columns={[
            { key: 'strategy_id', label: '策略' },
            { key: 'version', label: '版本' },
            { key: 'period', label: '期間' },
            { key: 'total_return', label: '累積報酬',
              render: (r) => <Pnl value={r.total_return} /> },
            { key: 'annualized_return', label: '年化' },
            { key: 'max_drawdown', label: '最大回撤' },
            { key: 'volatility', label: '波動率' },
            { key: 'trades', label: '交易次數' },
            { key: 'win_rate', label: '勝率' },
            { key: 'sel', label: '',
              render: (r) => <button onClick={() => setSel(r)}>檢視</button> },
          ]}
          empty={<EmptyState detail="尚無回測結果鏡像。" />}
        />
      </Section>

      {detail && (
        <Section title={`結果詳細：${String((detail as any).strategy_id || '')}`}>
          <div className="inv-metric-grid">
            {['total_return', 'annualized_return', 'max_drawdown',
              'volatility', 'trades', 'win_rate'].map((k) => (
              <Metric key={k} label={k}
                value={num((detail as any)[k]).toFixed(2)} />
            ))}
          </div>
          <InvestmentChart kind="area" title="資金曲線" points={curve}
            empty="無資金曲線資料" />
          <DataTable
            rows={arr((detail as any)?.trades_detail || (detail as any)?.trades_list)}
            columns={[
              { key: 'at', label: '時間' },
              { key: 'side', label: '方向' },
              { key: 'quantity', label: '數量' },
              { key: 'price', label: '價格' },
              { key: 'pnl', label: '損益', render: (r) => <Pnl value={r.pnl} /> },
            ]}
            empty={<EmptyState kind="insufficient" detail="無交易明細。" />}
          />
        </Section>
      )}

      <Section title="驗證與比較">
        <div className="inv-cards">
          {['版本比較', '樣本內驗證', '樣本外驗證', 'Walk-Forward',
            'SHADOW 結果', 'PAPER 結果'].map((k) => (
            <article key={k} className="inv-card">
              <b>{k}</b>
              <EmptyState kind="insufficient"
                detail="對應驗證資料送達後自動顯示。" />
            </article>
          ))}
        </div>
        <p className="inv-note">
          星澄可解讀回測結果（經 AI 研究通道）；模型不會生成不存在的績效數字。
        </p>
      </Section>
    </div>
  )
}
