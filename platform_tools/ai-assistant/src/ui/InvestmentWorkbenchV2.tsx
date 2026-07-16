import { useEffect, useMemo, useState } from 'react'
import {
  formatInvestmentClock,
  formatInvestmentNumber,
  investmentCodeLabel,
  openInvestmentFile,
  type InvestmentAnalyticsSnapshot,
  type InvestmentHolding,
  type InvestmentV3Snapshot,
  type MobileSyncStatus,
} from './investmentWatchFeature'

type InvestmentResult = Record<string, unknown> & {
  ok?: boolean
  message?: string
}

type Props = {
  analytics: InvestmentAnalyticsSnapshot | null
  v3: InvestmentV3Snapshot | null
  portfolio: {
    source_created_at?: string
    imported_at?: string
  } | null
  holdings: InvestmentHolding[]
  mobileSync: MobileSyncStatus | null
  busyAction: string
  runCommand: (
    command: string,
    payload: Record<string, unknown>,
    label: string,
    timeoutMs?: number,
    requireHoldings?: boolean
  ) => Promise<InvestmentResult | null>
}

type TabKey =
  | 'overview'
  | 'ledger'
  | 'lab'
  | 'allocation'
  | 'events'
  | 'operations'
  | 'journal'

const TABS: Array<{ key: TabKey; label: string }> = [
  { key: 'overview', label: '今日總覽' },
  { key: 'ledger', label: '交易帳本' },
  { key: 'lab', label: '研究實驗室' },
  { key: 'allocation', label: '配置模擬' },
  { key: 'events', label: '事件與警示' },
  { key: 'operations', label: '自動化與備援' },
  { key: 'journal', label: '決策與安全' },
]

function text(value: unknown, fallback = '-'): string {
  const normalized = String(value ?? '').trim()
  return normalized || fallback
}

function number(value: unknown, fallback = 0): number {
  const normalized = typeof value === 'number' ? value : Number(value)
  return Number.isFinite(normalized) ? normalized : fallback
}

function money(value: unknown): string {
  const normalized = Number(value)
  return Number.isFinite(normalized)
    ? `NT$ ${formatInvestmentNumber(normalized, 2)}`
    : '-'
}

function percent(value: unknown): string {
  const normalized = Number(value)
  return Number.isFinite(normalized)
    ? `${formatInvestmentNumber(normalized, 2)}%`
    : '-'
}

function localDateTimeValue(): string {
  const now = new Date(Date.now() - new Date().getTimezoneOffset() * 60_000)
  return now.toISOString().slice(0, 16)
}

function localDateValue(value?: string): string {
  const fallback = new Date(Date.now() - 365 * 24 * 60 * 60 * 1000)
  const parsed = value ? new Date(value) : fallback
  const date = Number.isFinite(parsed.getTime()) ? parsed : fallback
  const local = new Date(date.getTime() - date.getTimezoneOffset() * 60_000)
  return local.toISOString().slice(0, 10)
}

function EquityCurve({
  points,
}: {
  points: Array<{ date?: string; value?: number }>
}) {
  const path = useMemo(() => {
    const values = points
      .map((item) => number(item.value))
      .filter((value) => value > 0)
    if (values.length < 2) return ''
    const minimum = Math.min(...values)
    const maximum = Math.max(...values)
    const span = maximum - minimum || 1
    return values
      .map((value, index) => {
        const x = 12 + (index / (values.length - 1)) * 656
        const y = 126 - ((value - minimum) / span) * 108
        return `${index === 0 ? 'M' : 'L'} ${x.toFixed(2)} ${y.toFixed(2)}`
      })
      .join(' ')
  }, [points])

  if (!path)
    return (
      <p className="investment-v2-empty">累積兩筆組合快照後顯示權益曲線。</p>
    )
  return (
    <svg
      className="investment-v2-chart"
      viewBox="0 0 680 140"
      role="img"
      aria-label="投資組合權益曲線"
      preserveAspectRatio="none"
    >
      <line x1="12" y1="126" x2="668" y2="126" />
      <line x1="12" y1="18" x2="12" y2="126" />
      <path d={path} />
    </svg>
  )
}

export function InvestmentWorkbenchV2({
  analytics,
  v3,
  portfolio,
  holdings,
  mobileSync,
  busyAction,
  runCommand,
}: Props) {
  const [tab, setTab] = useState<TabKey>('overview')
  const [transaction, setTransaction] = useState({
    side: 'BUY',
    symbol: holdings[0]?.symbol || '',
    quantity: '1',
    price: '',
    fee: '0',
    tax: '0',
    currency: holdings[0]?.currency || 'TWD',
    occurred_at: localDateTimeValue(),
    note: '',
  })
  const [eventDraft, setEventDraft] = useState({
    event_type: 'earnings',
    symbol: holdings[0]?.symbol || '',
    title: '',
    scheduled_at: localDateTimeValue(),
  })
  const [alertDraft, setAlertDraft] = useState({
    name: '價格跌破警示',
    rule_type: 'price_below',
    symbol: holdings[0]?.symbol || '',
    operator: '<=',
    threshold: '',
    severity: 'warning',
  })
  const [strategy, setStrategy] = useState('buy_and_hold')
  const [cashReserve, setCashReserve] = useState('5')
  const [maxPosition, setMaxPosition] = useState('35')
  const [baseCurrency, setBaseCurrency] = useState('TWD')
  const [benchmark, setBenchmark] = useState('SPY')
  const [investmentPolicy, setInvestmentPolicy] = useState({
    investment_goal: '',
    time_horizon_years: '5',
    risk_capacity: 'balanced',
    cash_need_percent: '0',
    forbidden_assets: '',
    target_return_percent: '',
  })
  const [lastBacktest, setLastBacktest] = useState<Record<
    string,
    unknown
  > | null>(null)
  const [lastRebalance, setLastRebalance] = useState<Record<
    string,
    unknown
  > | null>(null)
  const [lastStress, setLastStress] = useState<Record<string, unknown> | null>(
    null
  )
  const [optimizationMethod, setOptimizationMethod] = useState('risk_parity')
  const [lastOptimization, setLastOptimization] = useState<Record<
    string,
    unknown
  > | null>(null)
  const [lastMonteCarlo, setLastMonteCarlo] = useState<Record<
    string,
    unknown
  > | null>(null)
  const [schedulerMinutes, setSchedulerMinutes] = useState('15')
  const [externalNotification, setExternalNotification] = useState({
    channel_id: 'email_smtp',
    enabled: false,
    host: '',
    port: '587',
    from: '',
    to: '',
    username: '',
    password: '',
    webhook_url: '',
  })
  const [lastBrokerImport, setLastBrokerImport] = useState<Record<
    string,
    unknown
  > | null>(null)
  const [openingDate, setOpeningDate] = useState(() =>
    localDateValue(portfolio?.source_created_at || portfolio?.imported_at)
  )

  const performance = analytics?.performance || {}
  const risk = analytics?.risk || {}
  const ledger = analytics?.ledger || {}
  const stress = lastStress || analytics?.stress || {}
  const transactions = ledger.transactions || []
  const ledgerReconciliation = (ledger.reconciliation || {}) as {
    status?: string
    symbol_count?: number
    matched_count?: number
    difference_count?: number
    coverage_percent?: number
    differences?: Array<Record<string, unknown>>
  }
  const applicableReconciliationCount = (
    ledgerReconciliation.differences || []
  ).filter((item) => {
    const suggestion =
      item.suggestion && typeof item.suggestion === 'object'
        ? (item.suggestion as Record<string, unknown>)
        : null
    return (
      suggestion &&
      number(suggestion.quantity) > 0 &&
      number(suggestion.price) > 0
    )
  }).length
  const events = analytics?.events || []
  const alertRules = analytics?.alerts?.rules || []
  const alertEvents = analytics?.alerts?.events || []
  const decisions = analytics?.decisions || []
  const calibration = analytics?.calibration || {}
  const dataCounts = analytics?.data_health?.counts || {}
  const dataWarnings = analytics?.data_health?.warnings || []
  const exposures = risk.exposures || {}
  const multiCurrency = v3?.multi_currency || {}
  const regime = v3?.regime || {}
  const factors = v3?.factors || {}
  const corporateActions = v3?.corporate_actions || {}
  const brokerImports = v3?.broker_imports || []
  const automation = v3?.automation || {}
  const notificationChannels = v3?.notifications?.channels || []
  const notificationOutbox = v3?.notifications?.outbox || []
  const modelGovernance = v3?.model_governance || {}
  const databaseSecurity = v3?.database_security || {}
  const backups = v3?.backups || []
  const auditLog = v3?.audit_log || []

  useEffect(() => {
    const policy = v3?.investment_policy
    if (!policy) return
    setInvestmentPolicy({
      investment_goal: text(policy.investment_goal, ''),
      time_horizon_years: text(policy.time_horizon_years, '5'),
      risk_capacity: text(policy.risk_capacity, 'balanced'),
      cash_need_percent: text(policy.cash_need_percent, '0'),
      forbidden_assets: (policy.forbidden_assets || []).join(', '),
      target_return_percent:
        policy.target_return_percent == null
          ? ''
          : text(policy.target_return_percent, ''),
    })
  }, [v3?.investment_policy])

  useEffect(() => {
    const knownDate =
      ledger.opening_ledger?.occurred_at || portfolio?.source_created_at
    if (knownDate) setOpeningDate(localDateValue(knownDate))
  }, [ledger.opening_ledger?.occurred_at, portfolio?.source_created_at])

  const busy = Boolean(busyAction)
  const actionBusy = (command: string) => busyAction === `investment:${command}`
  const handleTabKeyDown = (
    event: React.KeyboardEvent<HTMLButtonElement>,
    current: TabKey
  ) => {
    const currentIndex = TABS.findIndex((item) => item.key === current)
    let nextIndex = currentIndex
    if (event.key === 'ArrowRight') nextIndex = (currentIndex + 1) % TABS.length
    else if (event.key === 'ArrowLeft')
      nextIndex = (currentIndex - 1 + TABS.length) % TABS.length
    else if (event.key === 'Home') nextIndex = 0
    else if (event.key === 'End') nextIndex = TABS.length - 1
    else return
    event.preventDefault()
    const next = TABS[nextIndex]
    setTab(next.key)
    window.requestAnimationFrame(() =>
      document.getElementById(`investment-tab-${next.key}`)?.focus()
    )
  }

  const syncIntelligence = async () => {
    await runCommand(
      'investment_watch_sync_intelligence',
      { period: '1y' },
      '市場情報同步',
      240000,
      true
    )
  }

  const saveInvestmentPolicy = async (event: React.FormEvent) => {
    event.preventDefault()
    await runCommand(
      'investment_watch_update_v2_settings',
      {
        investment_goal: investmentPolicy.investment_goal.trim(),
        time_horizon_years: number(investmentPolicy.time_horizon_years, 5),
        risk_capacity: investmentPolicy.risk_capacity,
        cash_need_percent: number(investmentPolicy.cash_need_percent),
        forbidden_assets: investmentPolicy.forbidden_assets
          .split(',')
          .map((item) => item.trim().toUpperCase())
          .filter(Boolean),
        target_return_percent: investmentPolicy.target_return_percent
          ? number(investmentPolicy.target_return_percent)
          : null,
      },
      '投資政策更新'
    )
  }

  const addTransaction = async (event: React.FormEvent) => {
    event.preventDefault()
    const result = await runCommand(
      'investment_watch_add_transaction',
      {
        ...transaction,
        quantity: number(transaction.quantity),
        price: number(transaction.price),
        amount: number(transaction.price),
        fee: number(transaction.fee),
        tax: number(transaction.tax),
      },
      '交易入帳'
    )
    if (result) {
      setTransaction((current) => ({
        ...current,
        price: '',
        fee: '0',
        tax: '0',
        note: '',
      }))
    }
  }

  const seedOpeningLedger = async () => {
    if (!openingDate) return
    if (
      !window.confirm(
        `將以 ${openingDate} 的目前持股與新台幣本金建立估算期初帳本。這不是券商成交紀錄，確定繼續？`
      )
    )
      return
    await runCommand(
      'investment_watch_seed_opening_ledger',
      { occurred_at: `${openingDate}T00:00:00+08:00` },
      '建立估算期初帳本',
      120000,
      true
    )
  }

  const reconcileLedger = async () => {
    await runCommand(
      'investment_watch_reconcile_ledger',
      {},
      '持股帳本對帳',
      60000,
      true
    )
  }

  const applyLedgerReconciliation = async () => {
    const count = applicableReconciliationCount
    if (
      count <= 0 ||
      !window.confirm(
        `將依目前持股建立 ${count} 筆估算差額交易。這不是券商成交紀錄，確定繼續？`
      )
    )
      return
    await runCommand(
      'investment_watch_apply_ledger_reconciliation',
      { confirmed: true },
      '帳本差額調整',
      120000,
      true
    )
  }

  const runStress = async () => {
    const result = await runCommand(
      'investment_watch_run_stress_test',
      {},
      '壓力測試',
      60000,
      true
    )
    if (result?.stress_test && typeof result.stress_test === 'object') {
      setLastStress(result.stress_test as Record<string, unknown>)
    }
  }

  const runBacktest = async () => {
    const result = await runCommand(
      'investment_watch_run_backtest',
      {
        strategy,
        initial_capital: 1_000_000,
        fee_percent: 0.1425,
        slippage_percent: 0.05,
      },
      '策略回測',
      120000,
      true
    )
    if (result?.backtest && typeof result.backtest === 'object') {
      setLastBacktest(result.backtest as Record<string, unknown>)
    }
  }

  const planRebalance = async () => {
    const target = holdings.length > 0 ? 100 / holdings.length : 0
    const targets = Object.fromEntries(
      holdings
        .map((holding) => [text(holding.symbol, ''), target])
        .filter(([symbol]) => symbol)
    )
    const result = await runCommand(
      'investment_watch_plan_rebalance',
      {
        targets,
        cash_reserve_percent: number(cashReserve, 5),
        max_position_percent: number(maxPosition, 35),
        min_trade_value: 1000,
      },
      '再平衡模擬',
      60000,
      true
    )
    if (result?.rebalance && typeof result.rebalance === 'object') {
      setLastRebalance(result.rebalance as Record<string, unknown>)
    }
  }

  const addEvent = async (event: React.FormEvent) => {
    event.preventDefault()
    const result = await runCommand(
      'investment_watch_add_event',
      eventDraft,
      '事件建立'
    )
    if (result) setEventDraft((current) => ({ ...current, title: '' }))
  }

  const addAlert = async (event: React.FormEvent) => {
    event.preventDefault()
    await runCommand(
      'investment_watch_add_alert_rule',
      { ...alertDraft, threshold: number(alertDraft.threshold) },
      '警示規則建立'
    )
  }

  const importBrokerStatement = async () => {
    const file = await openInvestmentFile()
    if (!file) return
    const result = await runCommand(
      'investment_watch_import_broker_statement',
      { path: file },
      '券商明細比對',
      120000
    )
    if (result?.broker_import && typeof result.broker_import === 'object') {
      setLastBrokerImport(result.broker_import as Record<string, unknown>)
    }
  }

  const approveBrokerDifferences = async () => {
    const current = lastBrokerImport
    const rows = Array.isArray(current?.rows)
      ? (current?.rows as Array<Record<string, unknown>>)
      : []
    const rowIds = rows
      .filter((item) => item.match_status === 'unmatched')
      .map((item) => text(item.row_id, ''))
      .filter(Boolean)
    if (!current || rowIds.length === 0) return
    const result = await runCommand(
      'investment_watch_approve_broker_rows',
      { import_id: current.import_id, row_ids: rowIds, confirmed: true },
      '券商差異入帳'
    )
    if (result?.broker_import && typeof result.broker_import === 'object') {
      setLastBrokerImport(result.broker_import as Record<string, unknown>)
    }
  }

  const runOptimization = async () => {
    const result = await runCommand(
      'investment_watch_optimize_portfolio',
      {
        method: optimizationMethod,
        max_position_percent: number(maxPosition, 35),
        max_turnover_percent: 40,
      },
      '配置最佳化',
      120000,
      true
    )
    if (result?.optimization && typeof result.optimization === 'object') {
      setLastOptimization(result.optimization as Record<string, unknown>)
    }
  }

  const runMonteCarlo = async () => {
    const result = await runCommand(
      'investment_watch_run_monte_carlo',
      { simulations: 2000, horizon_days: 252, seed: 73021 },
      '蒙地卡羅模擬',
      120000,
      true
    )
    if (result?.monte_carlo && typeof result.monte_carlo === 'object') {
      setLastMonteCarlo(result.monte_carlo as Record<string, unknown>)
    }
  }

  const configureScheduler = async () => {
    await runCommand(
      'investment_watch_configure_scheduler',
      { interval_seconds: Math.max(1, number(schedulerMinutes, 15)) * 60 },
      '背景排程設定'
    )
  }

  const createBackup = async () => {
    await runCommand(
      'investment_watch_create_backup',
      { label: 'manual-ui' },
      '加密備份'
    )
  }

  const rotateDatabaseKey = async () => {
    await runCommand(
      'investment_watch_rotate_database_key',
      { confirmed: true },
      '資料庫換鑰',
      60000
    )
  }

  const restoreLatestBackup = async () => {
    const latest = backups[0]
    if (!latest || !window.confirm(`確定還原 ${text(latest.name)}？目前資料會先自動備份。`)) return
    await runCommand(
      'investment_watch_restore_backup',
      { backup_name: latest.name, confirmed: true },
      '資料庫還原',
      60000
    )
  }

  const runSchedulerNow = async () => {
    await runCommand(
      'investment_watch_run_scheduler',
      {},
      '背景監測',
      300000
    )
  }

  const toggleWindowsNotifications = async () => {
    const windowsChannel = notificationChannels.find(
      (item) => item.channel_id === 'windows_local'
    )
    const enabled = !Boolean(windowsChannel?.enabled)
    await runCommand(
      'investment_watch_configure_notification',
      { channel_id: 'windows_local', enabled, confirmed: enabled, config: {} },
      'Windows 通知設定'
    )
  }

  const saveExternalNotification = async (event: React.FormEvent) => {
    event.preventDefault()
    const config =
      externalNotification.channel_id === 'email_smtp'
        ? {
            host: externalNotification.host,
            port: number(externalNotification.port, 587),
            from: externalNotification.from,
            to: externalNotification.to,
            username: externalNotification.username,
            password: externalNotification.password,
            starttls: true,
          }
        : { webhook_url: externalNotification.webhook_url }
    const result = await runCommand(
      'investment_watch_configure_notification',
      {
        channel_id: externalNotification.channel_id,
        enabled: externalNotification.enabled,
        confirmed: externalNotification.enabled,
        config,
      },
      '外部通知設定'
    )
    if (result) {
      setExternalNotification((current) => ({ ...current, password: '' }))
    }
  }

  return (
    <section className="investment-v2" aria-label="AI 投資管家 4.0 分析工作台">
      <div className="investment-v2-head">
        <div>
          <span>PORTFOLIO OS 4.0</span>
          <strong>投資分析工作台</strong>
          <p>
            行情 {dataCounts.prices || 0} · 交易 {dataCounts.transactions || 0}{' '}
            · 快照 {dataCounts.portfolio_snapshots || 0} · 警示{' '}
            {analytics?.alerts?.unacknowledged_count || 0}
          </p>
        </div>
        <div className="investment-v2-head-actions">
          <button
            type="button"
            onClick={() => void syncIntelligence()}
            disabled={busy || holdings.length === 0}
          >
            {actionBusy('investment_watch_sync_intelligence')
              ? '同步中...'
              : '同步市場情報'}
          </button>
          <span
            className={`investment-v2-health investment-v2-health--${risk.status || 'empty'}`}
          >
            {analytics?.data_health?.history_ready
              ? '歷史資料就緒'
              : '等待歷史資料'}
          </span>
        </div>
      </div>

      <div
        className="investment-v2-tabs"
        role="tablist"
        aria-label="分析工作台分頁"
      >
        {TABS.map((item) => (
          <button
            key={item.key}
            id={`investment-tab-${item.key}`}
            type="button"
            role="tab"
            aria-selected={tab === item.key}
            aria-controls={`investment-panel-${item.key}`}
            tabIndex={tab === item.key ? 0 : -1}
            className={tab === item.key ? 'is-active' : ''}
            onClick={() => setTab(item.key)}
            onKeyDown={(event) => handleTabKeyDown(event, item.key)}
          >
            {item.label}
          </button>
        ))}
      </div>

      {tab === 'overview' ? (
        <div
          id="investment-panel-overview"
          className="investment-v2-pane"
          role="tabpanel"
          aria-labelledby="investment-tab-overview"
          tabIndex={0}
        >
          <section
            className="investment-v2-panel investment-v2-panel--wide investment-v2-action-center"
            aria-label="今日資料品質與待辦"
          >
            <div className="investment-v2-panel-head">
              <strong>今天先看這裡</strong>
              <span>
                {dataWarnings.length === 0 && number(risk.sample_count) >= 20
                  ? '資料可用'
                  : '需要確認'}
              </span>
            </div>
            <div className="investment-v2-list">
              <div>
                <span>歷史樣本</span>
                <strong>{number(risk.sample_count)} 筆</strong>
              </div>
              <div>
                <span>資料新鮮度</span>
                <strong>
                  {formatInvestmentClock(analytics?.generated_at) || '尚未產生'}
                </strong>
              </div>
              <div>
                <span>帳本品質</span>
                <strong>{text(ledger.ledger_quality, '尚未建立')}</strong>
              </div>
              <div>
                <span>未確認警示</span>
                <strong>{analytics?.alerts?.unacknowledged_count || 0}</strong>
              </div>
            </div>
            {dataWarnings.length > 0 ? (
              <ul className="investment-v2-quality-warnings" aria-label="資料品質警告">
                {dataWarnings.slice(0, 5).map((warning) => (
                  <li key={warning}>{warning}</li>
                ))}
              </ul>
            ) : (
              <p className="investment-v2-empty">
                所有分析仍須依資料時間、覆蓋率與假設人工確認；系統不會自動送單。
              </p>
            )}
          </section>
          <div className="investment-v2-metrics">
            <div>
              <span>組合市值</span>
              <strong>{money(performance.current_value)}</strong>
            </div>
            <div>
              <span>未實現損益</span>
              <strong
                className={
                  number(performance.unrealized_pnl) >= 0
                    ? 'is-positive'
                    : 'is-negative'
                }
              >
                {money(performance.unrealized_pnl)}
              </strong>
              <small>{percent(performance.unrealized_pnl_percent)}</small>
            </div>
            <div>
              <span>TWR</span>
              <strong>{percent(performance.twr_percent)}</strong>
            </div>
            <div>
              <span>
                {ledger.ledger_quality === 'estimated_opening'
                  ? 'XIRR（估算）'
                  : 'XIRR'}
              </span>
              <strong>{percent(performance.xirr_percent)}</strong>
            </div>
            <div>
              <span>年化波動</span>
              <strong>{percent(risk.annualized_volatility_percent)}</strong>
            </div>
            <div>
              <span>單日 VaR 95%</span>
              <strong>{percent(risk.var_95_one_day_percent)}</strong>
            </div>
            <div>
              <span>最大回撤</span>
              <strong>{percent(risk.max_drawdown_percent)}</strong>
            </div>
            <div>
              <span>Beta · {text(risk.benchmark)}</span>
              <strong>{formatInvestmentNumber(risk.beta, 3)}</strong>
            </div>
            <div>
              <span>基準幣別市值 · {text(multiCurrency.base_currency)}</span>
              <strong>{money(multiCurrency.market_value_base)}</strong>
            </div>
            <div>
              <span>資產價格損益</span>
              <strong
                className={number(multiCurrency.asset_pnl_base) >= 0 ? 'is-positive' : 'is-negative'}
              >
                {money(multiCurrency.asset_pnl_base)}
              </strong>
            </div>
            <div>
              <span>匯率損益</span>
              <strong
                className={number(multiCurrency.currency_pnl_base) >= 0 ? 'is-positive' : 'is-negative'}
              >
                {money(multiCurrency.currency_pnl_base)}
              </strong>
            </div>
            <div>
              <span>市場狀態</span>
              <strong>{text(regime.label, '等待歷史資料')}</strong>
              <small>信心 {percent(number(regime.confidence) * 100)}</small>
            </div>
          </div>
          <div className="investment-v2-overview-grid">
            <section className="investment-v2-panel investment-v2-panel--wide">
              <div className="investment-v2-panel-head">
                <strong>權益曲線</strong>
                <span>{performance.snapshot_count || 0} 筆快照</span>
              </div>
              <EquityCurve points={performance.equity_curve || []} />
            </section>
            <section className="investment-v2-panel">
              <div className="investment-v2-panel-head">
                <strong>風險貢獻</strong>
                <span>{risk.sample_count || 0} 期</span>
              </div>
              <div className="investment-v2-list">
                {(risk.risk_contributions || [])
                  .slice(0, 6)
                  .map((item, index) => (
                    <div key={`${text(item.symbol)}:${index}`}>
                      <span>{text(item.symbol)}</span>
                      <strong>{percent(item.risk_contribution_percent)}</strong>
                    </div>
                  ))}
                {(risk.risk_contributions || []).length === 0 ? (
                  <p>同步歷史行情後顯示。</p>
                ) : null}
              </div>
            </section>
            <section className="investment-v2-panel">
              <div className="investment-v2-panel-head">
                <strong>曝險分布</strong>
                <span>市場 / 幣別</span>
              </div>
              <div className="investment-v2-list">
                {Object.entries(exposures.market || {}).map(([key, value]) => (
                  <div key={`market:${key}`}>
                    <span>{investmentCodeLabel(key)}</span>
                    <strong>{percent(value)}</strong>
                  </div>
                ))}
                {Object.entries(exposures.currency || {}).map(
                  ([key, value]) => (
                    <div key={`currency:${key}`}>
                      <span>{key}</span>
                      <strong>{percent(value)}</strong>
                    </div>
                  )
                )}
              </div>
            </section>
          </div>
          {(analytics?.data_health?.warnings || []).length > 0 ? (
            <div className="investment-v2-notice">
              {(analytics?.data_health?.warnings || []).map((warning) => (
                <span key={warning}>{warning}</span>
              ))}
            </div>
          ) : null}
        </div>
      ) : null}

      {tab === 'ledger' ? (
        <div
          id="investment-panel-ledger"
          className="investment-v2-pane investment-v2-ledger-layout"
          role="tabpanel"
          aria-labelledby="investment-tab-ledger"
          tabIndex={0}
        >
          <form
            className="investment-v2-form"
            onSubmit={(event) => void addTransaction(event)}
          >
            <div className="investment-v2-panel-head">
              <strong>新增交易</strong>
              <span>FIFO 成本法</span>
            </div>
            <label>
              類型
              <select
                value={transaction.side}
                onChange={(event) =>
                  setTransaction({ ...transaction, side: event.target.value })
                }
              >
                <option value="BUY">買進</option>
                <option value="SELL">賣出</option>
                <option value="DIVIDEND">股息</option>
                <option value="FEE">費用</option>
                <option value="CASH_IN">入金</option>
                <option value="CASH_OUT">出金</option>
              </select>
            </label>
            <label>
              標的
              <input
                value={transaction.symbol}
                onChange={(event) =>
                  setTransaction({
                    ...transaction,
                    symbol: event.target.value.toUpperCase(),
                  })
                }
              />
            </label>
            <div className="investment-v2-form-row">
              <label>
                數量
                <input
                  type="number"
                  min="0"
                  step="any"
                  value={transaction.quantity}
                  onChange={(event) =>
                    setTransaction({
                      ...transaction,
                      quantity: event.target.value,
                    })
                  }
                />
              </label>
              <label>
                價格 / 金額
                <input
                  type="number"
                  min="0"
                  step="any"
                  required
                  value={transaction.price}
                  onChange={(event) =>
                    setTransaction({
                      ...transaction,
                      price: event.target.value,
                    })
                  }
                />
              </label>
            </div>
            <div className="investment-v2-form-row">
              <label>
                手續費
                <input
                  type="number"
                  min="0"
                  step="any"
                  value={transaction.fee}
                  onChange={(event) =>
                    setTransaction({ ...transaction, fee: event.target.value })
                  }
                />
              </label>
              <label>
                交易稅
                <input
                  type="number"
                  min="0"
                  step="any"
                  value={transaction.tax}
                  onChange={(event) =>
                    setTransaction({ ...transaction, tax: event.target.value })
                  }
                />
              </label>
            </div>
            <div className="investment-v2-form-row">
              <label>
                幣別
                <input
                  value={transaction.currency}
                  onChange={(event) =>
                    setTransaction({
                      ...transaction,
                      currency: event.target.value.toUpperCase(),
                    })
                  }
                />
              </label>
              <label>
                時間
                <input
                  type="datetime-local"
                  value={transaction.occurred_at}
                  onChange={(event) =>
                    setTransaction({
                      ...transaction,
                      occurred_at: event.target.value,
                    })
                  }
                />
              </label>
            </div>
            <label>
              備註
              <input
                value={transaction.note}
                onChange={(event) =>
                  setTransaction({ ...transaction, note: event.target.value })
                }
              />
            </label>
            <button type="submit" className="nexus-primary" disabled={busy}>
              寫入帳本
            </button>
          </form>
          <section className="investment-v2-panel investment-v2-panel--table">
            <div className="investment-v2-panel-head">
              <strong>交易明細</strong>
              <span>{ledger.transaction_count || transactions.length} 筆</span>
            </div>
            <div className="investment-v3-toolbar investment-v3-toolbar--opening">
              <label>
                期初日期
                <input
                  type="date"
                  value={openingDate}
                  onChange={(event) => setOpeningDate(event.target.value)}
                />
              </label>
              <button
                type="button"
                className="nexus-primary"
                onClick={() => void seedOpeningLedger()}
                disabled={
                  busy ||
                  holdings.length === 0 ||
                  number(ledger.confirmed_transaction_count) > 0
                }
              >
                從持股建立期初帳本
              </button>
              <button
                type="button"
                onClick={() => void reconcileLedger()}
                disabled={busy || holdings.length === 0}
              >
                重新對帳
              </button>
              <button
                type="button"
                onClick={() => void applyLedgerReconciliation()}
                disabled={busy || applicableReconciliationCount <= 0}
              >
                建立差額調整
              </button>
            </div>
            <div className="investment-v2-ledger-quality">
              對帳覆蓋 {percent(ledgerReconciliation.coverage_percent)} · 相符{' '}
              {number(ledgerReconciliation.matched_count)} · 差異{' '}
              {number(ledgerReconciliation.difference_count)} · 可自動調整{' '}
              {applicableReconciliationCount}
            </div>
            {ledger.ledger_quality === 'estimated_opening' ? (
              <div className="investment-v2-ledger-quality">
                估算期初 {number(ledger.estimated_transaction_count)} 筆 · 覆蓋{' '}
                {percent(ledger.opening_ledger?.coverage_percent)} · 尚缺{' '}
                {number(ledger.opening_ledger?.uncovered_count)} 筆本金
              </div>
            ) : null}
            {Array.isArray(ledgerReconciliation.differences) &&
            ledgerReconciliation.differences.length > 0 ? (
              <div className="investment-v2-reconciliation-list">
                {ledgerReconciliation.differences.slice(0, 8).map((item, index) => (
                  <span key={`${text(item.symbol)}:${index}`}>
                    {text(item.symbol)} · 持股 {formatInvestmentNumber(item.holding_quantity, 4)} ·
                    帳本 {formatInvestmentNumber(item.ledger_quantity, 4)} · 差{' '}
                    {formatInvestmentNumber(item.difference_quantity, 4)}
                  </span>
                ))}
              </div>
            ) : null}
            <div className="investment-v2-summary-line">
              <span>已實現 {money(ledger.realized_pnl)}</span>
              <span>股息 {money(ledger.dividend_income)}</span>
              <span>費稅 {money(ledger.fees_and_taxes)}</span>
            </div>
            <div className="investment-v2-table-wrap">
              <table>
                <caption>投資交易帳本</caption>
                <thead>
                  <tr>
                    <th scope="col">時間</th>
                    <th scope="col">類型</th>
                    <th scope="col">標的</th>
                    <th scope="col">數量</th>
                    <th scope="col">價格</th>
                    <th scope="col">費稅</th>
                    <th scope="col">備註</th>
                    <th scope="col">操作</th>
                  </tr>
                </thead>
                <tbody>
                  {transactions.map((item, index) => (
                    <tr key={text(item.transaction_id, String(index))}>
                      <td>
                        {formatInvestmentClock(text(item.occurred_at, '')) ||
                          '-'}
                      </td>
                      <td>{text(item.side)}</td>
                      <td>{text(item.symbol)}</td>
                      <td>{money(item.quantity)}</td>
                      <td>{money(item.price)}</td>
                      <td>{money(number(item.fee) + number(item.tax))}</td>
                      <td>{text(item.note)}</td>
                      <td>
                        <button
                          type="button"
                          className="nexus-danger"
                          title={
                            item.is_estimated
                              ? '估算期初交易請由上方按鈕統一重建'
                              : '刪除交易'
                          }
                          onClick={() => {
                            if (item.is_estimated) return
                            if (window.confirm('確定刪除這筆交易？'))
                              void runCommand(
                                'investment_watch_delete_transaction',
                                { transaction_id: item.transaction_id },
                                '交易刪除'
                              )
                          }}
                          disabled={busy || Boolean(item.is_estimated)}
                        >
                          {item.is_estimated ? '期初' : '刪除'}
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {transactions.length === 0 ? (
                <p className="investment-v2-empty">尚未建立交易帳本。</p>
              ) : null}
            </div>
          </section>
          <section className="investment-v2-panel investment-v2-panel--wide">
            <div className="investment-v2-panel-head">
              <strong>券商明細對帳</strong>
              <span>{brokerImports.length} 個批次</span>
            </div>
            <div className="investment-v3-toolbar">
              <button
                type="button"
                onClick={() => void importBrokerStatement()}
                disabled={busy}
              >
                匯入 CSV / Excel / PDF
              </button>
              <button
                type="button"
                className="nexus-primary"
                onClick={() => {
                  if (window.confirm('確定將目前所有未配對差異寫入交易帳本？'))
                    void approveBrokerDifferences()
                }}
                disabled={
                  busy ||
                  !Array.isArray(lastBrokerImport?.rows) ||
                  !(lastBrokerImport?.rows as Array<Record<string, unknown>>).some(
                    (item) => item.match_status === 'unmatched'
                  )
                }
              >
                確認差異入帳
              </button>
              <span>原始檔以短暫快照讀取，不保持 Excel 鎖定</span>
            </div>
            {lastBrokerImport ? (
              <div className="investment-v2-table-wrap">
                <table>
                  <caption>券商匯入配對結果</caption>
                  <thead>
                    <tr>
                      <th scope="col">日期</th>
                      <th scope="col">狀態</th>
                      <th scope="col">標的</th>
                      <th scope="col">類型</th>
                      <th scope="col">數量</th>
                      <th scope="col">價格</th>
                    </tr>
                  </thead>
                  <tbody>
                    {(
                      (lastBrokerImport.rows as
                        | Array<Record<string, unknown>>
                        | undefined) || []
                    ).map((item, index) => (
                      <tr key={text(item.row_id, String(index))}>
                        <td>{formatInvestmentClock(text(item.occurred_at, ''))}</td>
                        <td>{text(item.match_status)}</td>
                        <td>{text(item.symbol)}</td>
                        <td>{text(item.side)}</td>
                        <td>{money(item.quantity)}</td>
                        <td>{money(item.price)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <p className="investment-v2-empty">匯入後只顯示配對結果與差異，不會自動修改帳本。</p>
            )}
          </section>
        </div>
      ) : null}

      {tab === 'lab' ? (
        <div
          id="investment-panel-lab"
          className="investment-v2-pane"
          role="tabpanel"
          aria-labelledby="investment-tab-lab"
          tabIndex={0}
        >
          <div className="investment-v2-lab-tools">
            <section>
              <strong>壓力測試</strong>
              <span>預設四種市場衝擊</span>
              <button
                type="button"
                onClick={() => void runStress()}
                disabled={busy || holdings.length === 0}
              >
                重新計算
              </button>
            </section>
            <section>
                <strong>策略回測</strong>
                <select
                  aria-label="回測策略"
                  value={strategy}
                  onChange={(event) => setStrategy(event.target.value)}
              >
                <option value="buy_and_hold">買入持有</option>
                <option value="equal_weight">等權重</option>
                <option value="momentum">動能輪動</option>
              </select>
              <button
                type="button"
                onClick={() => void runBacktest()}
                disabled={busy || holdings.length === 0}
              >
                執行回測
              </button>
            </section>
            <section>
              <strong>再平衡草案</strong>
              <div>
                <label>
                  現金
                  <input
                    type="number"
                    value={cashReserve}
                    onChange={(event) => setCashReserve(event.target.value)}
                  />
                </label>
                <label>
                  單檔上限
                  <input
                    type="number"
                    value={maxPosition}
                    onChange={(event) => setMaxPosition(event.target.value)}
                  />
                </label>
              </div>
              <button
                type="button"
                onClick={() => void planRebalance()}
                disabled={busy || holdings.length === 0}
              >
                模擬等權重
              </button>
            </section>
          </div>
          <div className="investment-v2-scenarios">
            {(
              (stress.scenarios as
                Array<Record<string, unknown>> | undefined) || []
            ).map((scenario, index) => (
              <article key={`${text(scenario.name)}:${index}`}>
                <span>{text(scenario.name)}</span>
                <strong
                  className={
                    number(scenario.impact) >= 0 ? 'is-positive' : 'is-negative'
                  }
                >
                  {money(scenario.impact)}
                </strong>
                <small>
                  {percent(scenario.impact_percent)} · 模擬市值{' '}
                  {money(scenario.projected_value)}
                </small>
              </article>
            ))}
          </div>
          {lastBacktest ? (
            <section className="investment-v2-result">
              <div className="investment-v2-panel-head">
                <strong>回測結果 · {text(lastBacktest.strategy)}</strong>
                <span>{text(lastBacktest.sample_count)} 期</span>
              </div>
              <div>
                <span>
                  總報酬{' '}
                  <strong>{percent(lastBacktest.total_return_percent)}</strong>
                </span>
                <span>
                  年化{' '}
                  <strong>
                    {percent(lastBacktest.annualized_return_percent)}
                  </strong>
                </span>
                <span>
                  Sharpe{' '}
                  <strong>
                    {formatInvestmentNumber(lastBacktest.sharpe_ratio, 3)}
                  </strong>
                </span>
                <span>
                  最大回撤{' '}
                  <strong>{percent(lastBacktest.max_drawdown_percent)}</strong>
                </span>
              </div>
              <p>
                {text(
                  lastBacktest.methodology || lastBacktest.lookahead_protection,
                  '方法與資料覆蓋尚未回報'
                )}
              </p>
              {Array.isArray(lastBacktest.assumptions) ? (
                <ul className="investment-v2-quality-warnings">
                  {lastBacktest.assumptions.map((assumption) => (
                    <li key={text(assumption)}>{text(assumption)}</li>
                  ))}
                </ul>
              ) : null}
              {Array.isArray(lastBacktest.limitations) ? (
                <ul className="investment-v2-quality-warnings">
                  {lastBacktest.limitations.map((limitation) => (
                    <li key={text(limitation)}>{text(limitation)}</li>
                  ))}
                </ul>
              ) : null}
            </section>
          ) : null}
          {lastRebalance ? (
            <section className="investment-v2-result">
              <div className="investment-v2-panel-head">
                <strong>再平衡模擬</strong>
                <span>需人工核准，不會下單</span>
              </div>
              <div className="investment-v2-orders">
                {(
                  (lastRebalance.orders as
                    Array<Record<string, unknown>> | undefined) || []
                ).map((order, index) => (
                  <div key={`${text(order.symbol)}:${index}`}>
                    <strong>
                      {text(order.side)} {text(order.symbol)}
                    </strong>
                    <span>
                      {money(order.quantity)} 股 ·{' '}
                      {money(order.estimated_value)} · 目標{' '}
                      {percent(order.target_weight_percent)}
                    </span>
                  </div>
                ))}
              </div>
            </section>
          ) : null}
        </div>
      ) : null}

      {tab === 'allocation' ? (
        <div
          id="investment-panel-allocation"
          className="investment-v2-pane"
          role="tabpanel"
          aria-labelledby="investment-tab-allocation"
          tabIndex={0}
        >
          <div className="investment-v2-lab-tools investment-v3-allocation-tools">
            <section>
                <strong>配置模型</strong>
                <select
                  aria-label="配置模型"
                  value={optimizationMethod}
                onChange={(event) => setOptimizationMethod(event.target.value)}
              >
                <option value="risk_parity">風險平價</option>
                <option value="minimum_variance">最小變異</option>
                <option value="max_sharpe">最大夏普</option>
                <option value="black_litterman">Black-Litterman</option>
                <option value="cvar">CVaR 最小化</option>
              </select>
              <button
                type="button"
                onClick={() => void runOptimization()}
                disabled={busy || holdings.length < 2}
              >
                產生配置草案
              </button>
            </section>
            <section>
              <strong>蒙地卡羅</strong>
              <span>2,000 條 · 252 日 · 固定亂數種子</span>
              <button
                type="button"
                onClick={() => void runMonteCarlo()}
                disabled={busy || holdings.length === 0}
              >
                模擬未來分布
              </button>
            </section>
            <section>
              <strong>因子歸因</strong>
              <span>{text(factors.status, '等待行情')} · {text(factors.sample_count, '0')} 期</span>
              <span>市場、規模、價值、動能、品質代理因子</span>
            </section>
          </div>
          <div className="investment-v2-overview-grid">
            <section className="investment-v2-panel investment-v2-panel--wide">
              <div className="investment-v2-panel-head">
                <strong>最佳化權重</strong>
                <span>僅模擬，需人工核准</span>
              </div>
              {lastOptimization ? (
                <div className="investment-v2-table-wrap">
                  <table>
                    <caption>配置模擬權重比較</caption>
                    <thead>
                      <tr>
                        <th scope="col">標的</th>
                        <th scope="col">目前</th>
                        <th scope="col">草案</th>
                      </tr>
                    </thead>
                    <tbody>
                      {Object.entries(
                        (lastOptimization.weights as Record<string, number>) || {}
                      ).map(([symbol, value]) => (
                        <tr key={symbol}>
                          <td>{symbol}</td>
                          <td>
                            {percent(
                              (lastOptimization.current_weights as Record<string, number>)?.[
                                symbol
                              ]
                            )}
                          </td>
                          <td>{percent(value)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : (
                <p className="investment-v2-empty">同步足夠歷史行情後可建立受上限與換手率約束的配置草案。</p>
              )}
            </section>
            <section className="investment-v2-panel">
              <div className="investment-v2-panel-head">
                <strong>模擬分布</strong>
                <span>{text(lastMonteCarlo?.simulations, '0')} 條</span>
              </div>
              <div className="investment-v2-list">
                <div><span>5% 終值</span><strong>{money((lastMonteCarlo?.terminal_value as Record<string, unknown>)?.p05)}</strong></div>
                <div><span>中位終值</span><strong>{money((lastMonteCarlo?.terminal_value as Record<string, unknown>)?.p50)}</strong></div>
                <div><span>95% 終值</span><strong>{money((lastMonteCarlo?.terminal_value as Record<string, unknown>)?.p95)}</strong></div>
                <div><span>虧損機率</span><strong>{percent(lastMonteCarlo?.probability_of_loss_percent)}</strong></div>
              </div>
            </section>
            <section className="investment-v2-panel">
              <div className="investment-v2-panel-head">
                <strong>因子曝險</strong>
                <span>R² {formatInvestmentNumber(factors.r_squared, 3)}</span>
              </div>
              <div className="investment-v2-list">
                {(factors.factors || []).map((item, index) => (
                  <div key={`${text(item.factor)}:${index}`}>
                    <span>{text(item.factor)}</span>
                    <strong>{formatInvestmentNumber(item.exposure, 3)}</strong>
                  </div>
                ))}
                {(factors.factors || []).length === 0 ? <p>因子代理行情尚未齊全。</p> : null}
              </div>
            </section>
          </div>
        </div>
      ) : null}

      {tab === 'events' ? (
        <div
          id="investment-panel-events"
          className="investment-v2-pane investment-v2-events-grid"
          role="tabpanel"
          aria-labelledby="investment-tab-events"
          tabIndex={0}
        >
          <form
            className="investment-v2-form"
            onSubmit={(event) => void addEvent(event)}
          >
            <div className="investment-v2-panel-head">
              <strong>新增事件</strong>
              <span>財報 / 除息 / 總經</span>
            </div>
            <label>
              類型
              <select
                value={eventDraft.event_type}
                onChange={(event) =>
                  setEventDraft({
                    ...eventDraft,
                    event_type: event.target.value,
                  })
                }
              >
                <option value="earnings">財報</option>
                <option value="dividend">股息</option>
                <option value="economic">總經</option>
                <option value="news">新聞</option>
              </select>
            </label>
            <label>
              標的
              <input
                value={eventDraft.symbol}
                onChange={(event) =>
                  setEventDraft({
                    ...eventDraft,
                    symbol: event.target.value.toUpperCase(),
                  })
                }
              />
            </label>
            <label>
              標題
              <input
                required
                value={eventDraft.title}
                onChange={(event) =>
                  setEventDraft({ ...eventDraft, title: event.target.value })
                }
              />
            </label>
            <label>
              時間
              <input
                type="datetime-local"
                value={eventDraft.scheduled_at}
                onChange={(event) =>
                  setEventDraft({
                    ...eventDraft,
                    scheduled_at: event.target.value,
                  })
                }
              />
            </label>
            <button type="submit" className="nexus-primary" disabled={busy}>
              加入行事曆
            </button>
          </form>
          <form
            className="investment-v2-form"
            onSubmit={(event) => void addAlert(event)}
          >
            <div className="investment-v2-panel-head">
              <strong>新增持久警示</strong>
              <span>本機保存</span>
            </div>
            <label>
              名稱
              <input
                value={alertDraft.name}
                onChange={(event) =>
                  setAlertDraft({ ...alertDraft, name: event.target.value })
                }
              />
            </label>
            <label>
              條件
              <select
                value={alertDraft.rule_type}
                onChange={(event) =>
                  setAlertDraft({
                    ...alertDraft,
                    rule_type: event.target.value,
                  })
                }
              >
                <option value="price_below">價格跌破</option>
                <option value="price_above">價格突破</option>
                <option value="concentration">集中度</option>
                <option value="var">VaR</option>
                <option value="drawdown">回撤</option>
                <option value="event_window">事件天數</option>
              </select>
            </label>
            <div className="investment-v2-form-row">
              <label>
                標的
                <input
                  value={alertDraft.symbol}
                  onChange={(event) =>
                    setAlertDraft({
                      ...alertDraft,
                      symbol: event.target.value.toUpperCase(),
                    })
                  }
                />
              </label>
              <label>
                門檻
                <input
                  type="number"
                  step="any"
                  required
                  value={alertDraft.threshold}
                  onChange={(event) =>
                    setAlertDraft({
                      ...alertDraft,
                      threshold: event.target.value,
                    })
                  }
                />
              </label>
            </div>
            <label>
              嚴重度
              <select
                value={alertDraft.severity}
                onChange={(event) =>
                  setAlertDraft({ ...alertDraft, severity: event.target.value })
                }
              >
                <option value="info">資訊</option>
                <option value="warning">警告</option>
                <option value="critical">重大</option>
              </select>
            </label>
            <button type="submit" disabled={busy}>
              儲存警示
            </button>
          </form>
          <section className="investment-v2-panel">
            <div className="investment-v2-panel-head">
              <strong>事件情報</strong>
              <span>{events.length} 筆</span>
            </div>
            <div className="investment-v2-feed">
              {events.slice(0, 20).map((item, index) => (
                <article key={text(item.event_id, String(index))}>
                  <div>
                    <strong>
                      {text(item.symbol)} · {text(item.title)}
                    </strong>
                    <span>
                      {text(item.source)} ·{' '}
                      {formatInvestmentClock(text(item.scheduled_at, ''))}
                    </span>
                  </div>
                  <small>
                    情緒 {formatInvestmentNumber(item.sentiment, 2)} · 信心{' '}
                    {percent(number(item.confidence) * 100)}
                  </small>
                </article>
              ))}
              {events.length === 0 ? <p>尚無市場事件。</p> : null}
            </div>
          </section>
          <section className="investment-v2-panel">
            <div className="investment-v2-panel-head">
              <strong>警示中心</strong>
              <span>
                {analytics?.alerts?.unacknowledged_count || 0} 未確認 /{' '}
                {alertRules.length} 規則
              </span>
            </div>
            <div className="investment-v2-feed">
              {alertEvents.slice(0, 20).map((item, index) => (
                <article
                  key={text(item.alert_event_id, String(index))}
                  className={`is-${text(item.severity, 'info')}`}
                >
                  <div>
                    <strong>{text(item.title)}</strong>
                    <span>
                      {text(item.detail)} ·{' '}
                      {formatInvestmentClock(text(item.triggered_at, ''))}
                    </span>
                  </div>
                  {item.acknowledged_at ? (
                    <small>已確認</small>
                  ) : (
                    <button
                      type="button"
                      onClick={() =>
                        void runCommand(
                          'investment_watch_acknowledge_alert',
                          { alert_event_id: item.alert_event_id },
                          '警示確認'
                        )
                      }
                      disabled={busy}
                    >
                      確認
                    </button>
                  )}
                </article>
              ))}
              {alertEvents.length === 0 ? <p>目前沒有觸發警示。</p> : null}
            </div>
          </section>
          <section className="investment-v2-panel investment-v2-panel--wide">
            <div className="investment-v2-panel-head">
              <strong>公司行動資料治理</strong>
              <span>{text(corporateActions.pending_count, '0')} 筆待覆核</span>
            </div>
            <div className="investment-v2-feed">
              {(corporateActions.actions || []).slice(0, 20).map((item, index) => (
                <article key={text(item.action_id, String(index))}>
                  <div>
                    <strong>{text(item.symbol)} · {text(item.action_type)}</strong>
                    <span>{formatInvestmentClock(text(item.effective_at, ''))} · {text(item.source)} · {text(item.status)}</span>
                  </div>
                  {item.status === 'pending_review' || item.status === 'needs_correction' ? (
                    <div className="investment-v2-inline-actions">
                      <button
                        type="button"
                        onClick={() => void runCommand('investment_watch_review_corporate_action', { action_id: item.action_id, status: 'approved' }, '公司行動核准')}
                        disabled={busy || item.status === 'needs_correction'}
                      >
                        核准
                      </button>
                      <button
                        type="button"
                        onClick={() => void runCommand('investment_watch_review_corporate_action', { action_id: item.action_id, status: 'rejected' }, '公司行動拒絕')}
                        disabled={busy}
                      >
                        拒絕
                      </button>
                    </div>
                  ) : null}
                </article>
              ))}
              {(corporateActions.actions || []).length === 0 ? <p>同步到股息或拆股資料後會進入覆核區。</p> : null}
            </div>
          </section>
        </div>
      ) : null}

      {tab === 'operations' ? (
        <div
          id="investment-panel-operations"
          className="investment-v2-pane investment-v2-journal-grid"
          role="tabpanel"
          aria-labelledby="investment-tab-operations"
          tabIndex={0}
        >
          <section className="investment-v2-panel">
            <div className="investment-v2-panel-head">
              <strong>背景監測排程</strong>
              <span>{automation.running ? '執行中' : '待命'}</span>
            </div>
            <div className="investment-v3-control-stack">
              <label>
                週期（分鐘）
                <input
                  type="number"
                  min="1"
                  max="1440"
                  value={schedulerMinutes}
                  onChange={(event) => setSchedulerMinutes(event.target.value)}
                />
              </label>
              <div className="investment-v3-toolbar">
                <button type="button" onClick={() => void configureScheduler()} disabled={busy}>更新週期</button>
                <button type="button" onClick={() => void runSchedulerNow()} disabled={busy}>立即執行</button>
              </div>
              <span>最近狀態：{text((automation.last_run as Record<string, unknown>)?.status, '尚未執行')}</span>
            </div>
          </section>
          <section className="investment-v2-panel">
            <div className="investment-v2-panel-head">
              <strong>風險通知中心</strong>
              <span>{notificationOutbox.length} 筆</span>
            </div>
            <div className="investment-v3-control-stack">
              <label className="investment-v3-toggle">
                <input
                  type="checkbox"
                  checked={Boolean(notificationChannels.find((item) => item.channel_id === 'windows_local')?.enabled)}
                  onChange={() => void toggleWindowsNotifications()}
                  disabled={busy}
                />
                Windows 本機通知
              </label>
              <span>外部電子郵件、Teams 與 Slack 預設停用，需個別設定並明確同意。</span>
              <div className="investment-v2-list">
                {notificationOutbox.slice(0, 4).map((item, index) => (
                  <div key={text(item.notification_id, String(index))}>
                    <span>{text(item.title)}</span>
                    <strong>{text(item.status)}</strong>
                  </div>
                ))}
              </div>
            </div>
          </section>
          <form
            className="investment-v2-form"
            onSubmit={(event) => void saveExternalNotification(event)}
          >
            <div className="investment-v2-panel-head">
              <strong>外部通知管道</strong>
              <span>明確啟用後才傳送</span>
            </div>
            <label>
              管道
              <select
                value={externalNotification.channel_id}
                onChange={(event) =>
                  setExternalNotification({ ...externalNotification, channel_id: event.target.value })
                }
              >
                <option value="email_smtp">Email</option>
                <option value="teams_webhook">Microsoft Teams</option>
                <option value="slack_webhook">Slack</option>
              </select>
            </label>
            {externalNotification.channel_id === 'email_smtp' ? (
              <>
                <div className="investment-v2-form-row">
                  <label>
                    SMTP 主機
                    <input value={externalNotification.host} onChange={(event) => setExternalNotification({ ...externalNotification, host: event.target.value })} />
                  </label>
                  <label>
                    連接埠
                    <input type="number" value={externalNotification.port} onChange={(event) => setExternalNotification({ ...externalNotification, port: event.target.value })} />
                  </label>
                </div>
                <div className="investment-v2-form-row">
                  <label>
                    寄件者
                    <input type="email" value={externalNotification.from} onChange={(event) => setExternalNotification({ ...externalNotification, from: event.target.value })} />
                  </label>
                  <label>
                    收件者
                    <input type="email" value={externalNotification.to} onChange={(event) => setExternalNotification({ ...externalNotification, to: event.target.value })} />
                  </label>
                </div>
                <div className="investment-v2-form-row">
                  <label>
                    帳號
                    <input value={externalNotification.username} onChange={(event) => setExternalNotification({ ...externalNotification, username: event.target.value })} />
                  </label>
                  <label>
                    密碼
                    <input type="password" value={externalNotification.password} onChange={(event) => setExternalNotification({ ...externalNotification, password: event.target.value })} />
                  </label>
                </div>
              </>
            ) : (
              <label>
                Webhook URL
                <input type="url" value={externalNotification.webhook_url} onChange={(event) => setExternalNotification({ ...externalNotification, webhook_url: event.target.value })} />
              </label>
            )}
            <label className="investment-v3-toggle">
              <input
                type="checkbox"
                checked={externalNotification.enabled}
                onChange={(event) => setExternalNotification({ ...externalNotification, enabled: event.target.checked })}
              />
              啟用此管道
            </label>
            <button type="submit" disabled={busy}>儲存通知設定</button>
          </form>
          <section className="investment-v2-panel">
            <div className="investment-v2-panel-head">
              <strong>手機同步安全閘門</strong>
              <span>
                {mobileSync?.running
                  ? mobileSync.bind_host === '0.0.0.0'
                    ? '區網已開放'
                    : '僅限本機'
                  : '預設關閉'}
              </span>
            </div>
            <div className="investment-v3-control-stack">
              <p className="investment-v2-empty">
                配對碼不會放進網址；配對後改用裝置工作階段，閒置{' '}
                {mobileSync?.session_idle_minutes || 30} 分鐘即失效。
              </p>
              <div className="investment-v3-toolbar">
                <button
                  type="button"
                  onClick={() =>
                    void runCommand(
                      'investment_watch_set_mobile_sync_enabled',
                      { enabled: !mobileSync?.running, allow_lan: false },
                      mobileSync?.running ? '關閉手機同步' : '啟用本機手機同步'
                    )
                  }
                  disabled={busy}
                >
                  {mobileSync?.running ? '關閉手機同步' : '啟用（僅本機）'}
                </button>
                <button
                  type="button"
                  onClick={() => {
                    const allowLan = mobileSync?.bind_host !== '0.0.0.0'
                    if (
                      allowLan &&
                      !window.confirm(
                        '區網模式會讓同一網路裝置看見服務入口；仍需一次性配對碼。確定開放？'
                      )
                    )
                      return
                    void runCommand(
                      'investment_watch_set_mobile_sync_enabled',
                      { enabled: true, allow_lan: allowLan },
                      allowLan ? '開放區網手機同步' : '限制為本機同步'
                    )
                  }}
                  disabled={busy || !mobileSync?.running}
                >
                  {mobileSync?.bind_host === '0.0.0.0'
                    ? '改回僅本機'
                    : '明確開放區網'}
                </button>
              </div>
              <span>
                已配對工作階段 {mobileSync?.session_count || 0} ·{' '}
                {text(mobileSync?.loopback_url, '尚未啟用')}
              </span>
              {mobileSync?.start_error ? (
                <p className="investment-v2-quality-warnings" role="alert">
                  手機同步啟動失敗：{mobileSync.start_error}
                </p>
              ) : null}
            </div>
          </section>
          <section className="investment-v2-panel investment-v2-panel--wide">
            <div className="investment-v2-panel-head">
              <strong>加密資料庫與災難復原</strong>
              <span>{databaseSecurity.encrypted_at_rest ? '全庫加密' : '檢查保護狀態'}</span>
            </div>
            <div className="investment-v3-toolbar">
              <button type="button" onClick={() => void createBackup()} disabled={busy}>建立加密備份</button>
              <button type="button" onClick={() => void restoreLatestBackup()} disabled={busy || backups.length === 0}>還原最新備份</button>
              <button
                type="button"
                onClick={() => {
                  if (window.confirm('確定輪替資料庫保護金鑰？系統會先建立安全備份。')) void rotateDatabaseKey()
                }}
                disabled={busy}
              >
                輪替保護金鑰
              </button>
              <span>{backups.length} 份備份 · Key {text(databaseSecurity.key_id, '').slice(0, 8) || '-'}</span>
            </div>
            <div className="investment-v2-table-wrap">
              <table>
                <caption>投資資料安全稽核紀錄</caption>
                <thead><tr><th scope="col">時間</th><th scope="col">動作</th><th scope="col">等級</th></tr></thead>
                <tbody>
                  {auditLog.slice(0, 12).map((item, index) => (
                    <tr key={text(item.audit_id, String(index))}>
                      <td>{formatInvestmentClock(text(item.occurred_at, ''))}</td>
                      <td>{text(item.action)}</td>
                      <td>{text(item.severity)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>
        </div>
      ) : null}

      {tab === 'journal' ? (
        <div
          id="investment-panel-journal"
          className="investment-v2-pane investment-v2-journal-grid"
          role="tabpanel"
          aria-labelledby="investment-tab-journal"
          tabIndex={0}
        >
          <form
            className="investment-v2-form investment-v2-policy-form"
            onSubmit={saveInvestmentPolicy}
          >
            <div className="investment-v2-panel-head">
              <strong>個人投資政策（IPS）</strong>
              <span>約束分析與模擬，不會自動送單</span>
            </div>
            <label>
              投資目標
              <input
                value={investmentPolicy.investment_goal}
                placeholder="例如：十年退休資產累積"
                onChange={(event) =>
                  setInvestmentPolicy({
                    ...investmentPolicy,
                    investment_goal: event.target.value,
                  })
                }
              />
            </label>
            <div className="investment-v2-form-row">
              <label>
                投資期限（年）
                <input
                  type="number"
                  min="1"
                  max="80"
                  value={investmentPolicy.time_horizon_years}
                  onChange={(event) =>
                    setInvestmentPolicy({
                      ...investmentPolicy,
                      time_horizon_years: event.target.value,
                    })
                  }
                />
              </label>
              <label>
                風險承受能力
                <select
                  value={investmentPolicy.risk_capacity}
                  onChange={(event) =>
                    setInvestmentPolicy({
                      ...investmentPolicy,
                      risk_capacity: event.target.value,
                    })
                  }
                >
                  <option value="conservative">保守</option>
                  <option value="balanced">穩健</option>
                  <option value="growth">成長</option>
                  <option value="aggressive">積極</option>
                </select>
              </label>
            </div>
            <div className="investment-v2-form-row">
              <label>
                近期現金需求（%）
                <input
                  type="number"
                  min="0"
                  max="100"
                  value={investmentPolicy.cash_need_percent}
                  onChange={(event) =>
                    setInvestmentPolicy({
                      ...investmentPolicy,
                      cash_need_percent: event.target.value,
                    })
                  }
                />
              </label>
              <label>
                目標年報酬（選填）
                <input
                  type="number"
                  value={investmentPolicy.target_return_percent}
                  onChange={(event) =>
                    setInvestmentPolicy({
                      ...investmentPolicy,
                      target_return_percent: event.target.value,
                    })
                  }
                />
              </label>
            </div>
            <label>
              禁止資產／代碼（逗號分隔）
              <input
                value={investmentPolicy.forbidden_assets}
                placeholder="例如：CRYPTO, TSLA"
                onChange={(event) =>
                  setInvestmentPolicy({
                    ...investmentPolicy,
                    forbidden_assets: event.target.value,
                  })
                }
              />
            </label>
            <button type="submit" disabled={busy}>
              儲存投資政策
            </button>
          </form>
          <section className="investment-v2-panel investment-v2-panel--wide">
            <div className="investment-v2-panel-head">
              <strong>AI 決策日誌</strong>
              <span>{decisions.length} 筆</span>
            </div>
            <div className="investment-v2-feed">
              {decisions.slice(0, 20).map((item, index) => (
                <article key={text(item.decision_id, String(index))}>
                  <div>
                    <strong>
                      {text(item.symbol)} · {text(item.action)}
                    </strong>
                    <span>
                      {formatInvestmentClock(text(item.created_at, ''))} · 信心{' '}
                      {percent(number(item.confidence) * 100)} ·{' '}
                      {text(item.user_status)}
                    </span>
                  </div>
                  <div className="investment-v2-inline-actions">
                    <button
                      type="button"
                      onClick={() =>
                        void runCommand(
                          'investment_watch_set_decision_status',
                          { decision_id: item.decision_id, status: 'accepted' },
                          '決策接受'
                        )
                      }
                      disabled={busy}
                    >
                      接受
                    </button>
                    <button
                      type="button"
                      onClick={() =>
                        void runCommand(
                          'investment_watch_set_decision_status',
                          { decision_id: item.decision_id, status: 'rejected' },
                          '決策拒絕'
                        )
                      }
                      disabled={busy}
                    >
                      拒絕
                    </button>
                    <button
                      type="button"
                      onClick={() =>
                        void runCommand(
                          'investment_watch_set_decision_status',
                          { decision_id: item.decision_id, status: 'executed' },
                          '決策執行標記'
                        )
                      }
                      disabled={busy}
                    >
                      已執行
                    </button>
                  </div>
                </article>
              ))}
              {decisions.length === 0 ? (
                <p>執行星澄行動計畫後會建立決策日誌。</p>
              ) : null}
            </div>
          </section>
          <section className="investment-v2-panel">
            <div className="investment-v2-panel-head">
              <strong>模型校準</strong>
              <span>{text(calibration.calibration_label)}</span>
            </div>
            <div className="investment-v2-calibration">
              <strong>
                {calibration.brier_score == null
                  ? '-'
                  : formatInvestmentNumber(calibration.brier_score, 4)}
              </strong>
              <span>Brier Score</span>
              <p>
                已評估 {calibration.evaluated_count || 0} · 待追蹤{' '}
                {calibration.pending_count || 0}
              </p>
              <p>
                命中 {percent(calibration.accuracy_percent)} · 平均信心{' '}
                {percent(calibration.average_confidence_percent)}
              </p>
              <p>
                可靠度落差 {percent(calibration.reliability_gap_percent)} · 校準係數{' '}
                {formatInvestmentNumber(calibration.confidence_multiplier, 3)}
              </p>
            </div>
          </section>
          <section className="investment-v2-panel">
            <div className="investment-v2-panel-head">
              <strong>模型治理</strong>
              <span>{text(modelGovernance.status, 'ready')}</span>
            </div>
            <div className="investment-v2-list">
              <div><span>版本</span><strong>{text(modelGovernance.version_count, '0')}</strong></div>
              <div><span>分析執行</span><strong>{text(modelGovernance.run_count, '0')}</strong></div>
              <div><span>已校準執行</span><strong>{text(modelGovernance.evaluated_run_count, '0')}</strong></div>
              <div><span>平均 Brier</span><strong>{formatInvestmentNumber(modelGovernance.average_brier_score, 4)}</strong></div>
            </div>
            <p className="investment-v2-empty">
              校準品質低於護欄時自動切換唯讀，不產生新的執行建議。
            </p>
          </section>
          <form
            className="investment-v2-form"
            onSubmit={(event) => {
              event.preventDefault()
              void runCommand(
                'investment_watch_update_v2_settings',
                { base_currency: baseCurrency, benchmark },
                '分析設定更新'
              )
            }}
          >
            <div className="investment-v2-panel-head">
              <strong>分析設定</strong>
              <span>本機資料庫</span>
            </div>
            <label>
              基準幣別
              <input
                value={baseCurrency}
                onChange={(event) =>
                  setBaseCurrency(event.target.value.toUpperCase())
                }
              />
            </label>
            <label>
              比較基準
              <input
                value={benchmark}
                onChange={(event) =>
                  setBenchmark(event.target.value.toUpperCase())
                }
              />
            </label>
            <button type="submit" disabled={busy}>
              儲存設定
            </button>
          </form>
          <section className="investment-v2-panel">
            <div className="investment-v2-panel-head">
              <strong>本機隱私</strong>
              <span>
                {analytics?.privacy?.platform_protected
                  ? 'Windows 帳號保護'
                  : '檔案權限保護'}
              </span>
            </div>
            <div className="investment-v2-security">
              <span>狀態檔：{text(analytics?.privacy?.state_encryption)}</span>
              <span>
                敏感欄位：{text(analytics?.privacy?.sensitive_field_encryption)}
              </span>
              <span>
                配對期限：
                {formatInvestmentClock(mobileSync?.pairing_expires_at) || '-'}
              </span>
              <span>速率限制：{text(mobileSync?.rate_limit)}</span>
              <button
                type="button"
                className="nexus-danger"
                onClick={() =>
                  void runCommand(
                    'investment_watch_revoke_mobile_sync_pairing',
                    {},
                    '手機配對撤銷'
                  )
                }
                disabled={busy || mobileSync?.pairing_revoked}
              >
                撤銷手機配對
              </button>
            </div>
          </section>
        </div>
      ) : null}
    </section>
  )
}
