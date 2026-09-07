import {
  formatInvestmentClock,
  formatInvestmentNumber,
  type InvestmentAnalyticsSnapshot,
} from './investmentWatchFeature'

type InvestmentResult = Record<string, unknown> & {
  ok?: boolean
  message?: string
}

type StarAccountingPanelProps = {
  analytics: InvestmentAnalyticsSnapshot | null
  busyAction: string
  holdingCount: number
  runCommand: (
    command: string,
    payload: Record<string, unknown>,
    label: string,
    timeoutMs?: number,
    requireHoldings?: boolean
  ) => Promise<InvestmentResult | null>
}

function number(value: unknown): number {
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed : 0
}

function text(value: unknown, fallback = '-'): string {
  const normalized = String(value ?? '').trim()
  return normalized || fallback
}

export function StarAccountingPanel(props: StarAccountingPanelProps) {
  const { analytics, busyAction, holdingCount, runCommand } = props
  const ledger = analytics?.ledger || {}
  const reconciliation = analytics?.performance?.reconciliation || {}
  const transactions = Array.isArray(ledger.transactions)
    ? ledger.transactions.slice(0, 12)
    : []
  const busy = Boolean(busyAction)
  const differenceCount = number(reconciliation.difference_count)

  return (
    <section className="nexus-accounting" aria-label="AI帳務">
      <header className="nexus-accounting-head">
        <div>
          <p className="nexus-eyebrow">自主帳務</p>
          <h2>AI帳務</h2>
          <span>AI核對帳本與持股；AI投資管家驗證後才寫入專屬資料庫。</span>
        </div>
        <button
          type="button"
          className="nexus-primary"
          onClick={() =>
            void runCommand(
              'investment_watch_run_star_accounting',
              { trigger: 'accounting_workspace' },
              'AI自主帳務',
              120000,
              true
            )
          }
          disabled={busy || holdingCount === 0}
        >
          {busyAction === 'investment:investment_watch_run_star_accounting'
            ? 'AI處理中…'
            : '執行AI帳務'}
        </button>
      </header>

      <div className="nexus-accounting-metrics">
        <article>
          <span>帳本狀態</span>
          <strong>
            {ledger.ledger_quality === 'confirmed'
              ? '已確認'
              : ledger.ledger_quality === 'estimated_opening'
                ? '含估算期初'
                : '尚未建立'}
        </strong>
        </article>
        <article>
          <span>交易筆數</span>
          <strong>{number(ledger.transaction_count)}</strong>
        </article>
        <article className={differenceCount > 0 ? 'is-attention' : ''}>
          <span>待核對差異</span>
          <strong>{differenceCount}</strong>
        </article>
        <article>
          <span>對帳覆蓋</span>
          <strong>
            {formatInvestmentNumber(reconciliation.coverage_percent, 1)}%
          </strong>
        </article>
      </div>

      <section className="nexus-accounting-summary">
        <div>
          <span>已實現損益</span>
          <strong>NT$ {formatInvestmentNumber(ledger.realized_pnl, 2)}</strong>
        </div>
        <div>
          <span>股息收入</span>
          <strong>NT$ {formatInvestmentNumber(ledger.dividend_income, 2)}</strong>
        </div>
        <div>
          <span>費用與稅</span>
          <strong>NT$ {formatInvestmentNumber(ledger.fees_and_taxes, 2)}</strong>
        </div>
      </section>

      {differenceCount > 0 ? (
        <section className="nexus-accounting-notice" role="status">
          <strong>AI將自主檢查 {differenceCount} 筆差異</strong>
          <span>資料不一致、零價格或核准後狀態變動時會停止寫入。</span>
        </section>
      ) : null}

      <section className="nexus-accounting-ledger">
        <div className="nexus-section-head">
          <span>最近帳務紀錄</span>
          <strong>{transactions.length}</strong>
        </div>
        {transactions.length === 0 ? (
          <p className="nexus-empty-line">尚無帳務紀錄；匯入持股後由AI建立與維護。</p>
        ) : (
          <div className="nexus-accounting-list">
            {transactions.map((item, index) => (
              <article key={text(item.transaction_id, String(index))}>
                <div>
                  <strong>{text(item.symbol, '現金')}</strong>
                  <span>{text(item.side, '帳務')}</span>
                </div>
                <div>
                  <strong>
                    {formatInvestmentNumber(item.quantity, 4)} ×{' '}
                    {formatInvestmentNumber(item.price, 2)}
                  </strong>
                  <span>{formatInvestmentClock(text(item.occurred_at, '')) || '-'}</span>
                </div>
              </article>
            ))}
          </div>
        )}
      </section>
    </section>
  )
}
