import React from 'react'
import { useCommandQuery } from '../hooks'
import { DataFreshnessIndicator } from '../freshness'
import { DataTable, EmptyState, Metric, Section, Badge } from '../common'

type Props = { sendCommand: (c: string, p?: unknown) => { ok: boolean; message?: string } }

const arr = (v: unknown): Record<string, unknown>[] =>
  Array.isArray(v) ? (v as Record<string, unknown>[]) : []

export default function BrokerManagementPage({ sendCommand }: Props) {
  const status = useCommandQuery<any>(sendCommand, 'investment_broker_status')
  const accounts = useCommandQuery<any>(sendCommand, 'investment_broker_accounts')
  const sims = useCommandQuery<any>(sendCommand, 'investment_broker_sims')

  const s = (status.data || {}) as Record<string, any>
  const gate = s.offline_gate || s.gate || {}

  const brokerState = (b: Record<string, unknown>): string => {
    if (b.connected === true) return 'CONNECTED'
    if (b.mock === true || b.simulated === true) return 'MOCK'
    if (b.ready === true) return 'READY_FOR_INTEGRATION'
    return 'OFFLINE'
  }

  return (
    <div className="inv-page">
      <header className="inv-page-head">
        <h2>券商管理</h2>
        <span className="freshness freshness-unavailable">
          <b>本階段</b><i>真實券商連線停用</i>
        </span>
      </header>

      <div className="inv-metric-grid">
        <Metric label="券商網路" value={
          gate.broker_network_enabled ? '啟用' : 'OFFLINE'} tone="down" />
        <Metric label="真實交易" value={
          gate.live_trading_enabled ? '啟用' : '停用'} tone="down" />
        <Metric label="券商驗證" value={
          gate.broker_authentication_enabled ? '啟用' : '停用'} tone="down" />
        <Metric label="閘門狀態" value="OFFLINE_GATE_LOCKED"
          sub="前端無法切換——無 setter 暴露" />
      </div>

      <div className="inv-cards">
        {[
          { key: 'cathay', name: '國泰綜合證券', market: 'TW', type: '台股' },
          { key: 'fubon', name: '富邦綜合證券複委託', market: 'US', type: '美股複委託' },
        ].map((b) => {
          const rec = arr(accounts.data?.accounts).find(
            (a: any) =>
              String(a.broker_id || a.broker || '').toLowerCase().includes(b.key),
          ) || {}
          return (
            <article key={b.key} className="inv-card">
              <header className="inv-card-head">
                <b>{b.name}</b>
                <Badge text={brokerState(rec)}
                  tone={brokerState(rec) === 'OFFLINE' ? 'warn' : 'info'} />
              </header>
              <dl className="inv-card-meta">
                <dt>市場</dt><dd>{b.market}</dd>
                <dt>帳戶類型</dt><dd>{b.type}</dd>
                <dt>資料來源</dt>
                <dd>{String(rec.source || 'MANUAL / FILE_IMPORT')}</dd>
                <dt>整合狀態</dt>
                <dd>{String(rec.integration_status || brokerState(rec))}</dd>
              </dl>
              <DataFreshnessIndicator record={rec} />
            </article>
          )
        })}
      </div>

      <Section title="帳戶清單">
        <DataTable
          rows={arr(accounts.data?.accounts)}
          columns={[
            { key: 'account_id', label: '帳戶' },
            { key: 'broker_id', label: '券商' },
            { key: 'market', label: '市場' },
            { key: 'account_type', label: '類型' },
            { key: 'source', label: '資料來源' },
            { key: 'integration_status', label: '整合狀態' },
            { key: 'updated_at', label: '最後更新',
              render: (r) => <DataFreshnessIndicator record={r} /> },
          ]}
          empty={<EmptyState detail="尚無帳戶資料。" />}
        />
      </Section>

      <Section title="模擬券商（PAPER）">
        <DataTable
          rows={arr(sims.data?.simulations || sims.data?.accounts)}
          columns={[
            { key: 'account_id', label: '模擬帳戶' },
            { key: 'market', label: '市場' },
            { key: 'cash', label: '模擬資金' },
            { key: 'positions', label: '持倉數' },
            { key: 'status', label: '狀態' },
          ]}
          empty={<EmptyState detail="尚無模擬券商帳戶。" />}
        />
      </Section>

      <p className="inv-note">
        本階段不提供：真實登入、API 憑證輸入、真實下單、真實帳戶同步。
        broker_network_enabled 不可由前端切換。
      </p>
    </div>
  )
}
