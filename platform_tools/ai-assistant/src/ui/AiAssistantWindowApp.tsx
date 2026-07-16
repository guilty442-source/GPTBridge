import { useCallback, useEffect, useMemo, useState } from 'react'
import { useLocalBackendSocket } from './backendSocket'
import { useRef } from 'react'
import {
  formatInvestmentClock,
  formatInvestmentNumber,
  investmentCodeLabel,
  investmentRunLabel,
  investmentStatusLabel,
  openInvestmentFile,
  type ExcelHorizontalSheetPreview,
  type ExcelMappingPreview,
  type ExcelMappingSheetPreview,
  type InvestmentHolding,
  useInvestmentWatchFeature,
} from './investmentWatchFeature'
import { InvestmentWorkbenchV2 } from './InvestmentWorkbenchV2'
import './ai-assistant.css'

type InvestmentShellState = {
  ok?: boolean
  message?: string
  tool_root?: string
  state_path?: string
  local_only?: boolean
}

const LOCAL_AI_COMMAND_PRESETS = [
  {
    label: '投資評分',
    command: '連網 評分 全部持股，列出 100 分制、情境與操作策略',
  },
  {
    label: '防禦模式',
    command: '連網 防禦 高估值 通膨，檢查資產配置與單一持股上限',
  },
  {
    label: '集中度',
    command: '連網 配置 集中度 25% 風控優先，找出過度集中部位',
  },
  {
    label: '停損線',
    command: '連網 停損 8% 盤中 3%，列出需要預警的持股',
  },
  {
    label: 'ETF 檢查',
    command: '連網 ETF 同業比較 估值 技術面 籌碼，給出再平衡建議',
  },
]

type HoldingDraft = {
  holding_id: string
  symbol: string
  name: string
  market: string
  asset_type: string
  quantity: string
  average_cost: string
  currency: string
  principal_amount: string
  principal_currency: string
  principal_twd: string
  dividend_amount_twd: string
  dividend_per_unit: string
  monthly_dividend_twd: string
  annual_dividend_yield_percent: string
  payback_rate_percent: string
  current_value_twd: string
  fund_code: string
  fund_isin: string
  fund_share_class: string
  fund_quote_symbol: string
}

const EMPTY_HOLDING_DRAFT: HoldingDraft = {
  holding_id: '',
  symbol: '',
  name: '',
  market: 'TW',
  asset_type: 'STOCK',
  quantity: '1',
  average_cost: '0',
  currency: 'TWD',
  principal_amount: '',
  principal_currency: 'TWD',
  principal_twd: '',
  dividend_amount_twd: '',
  dividend_per_unit: '',
  monthly_dividend_twd: '',
  annual_dividend_yield_percent: '',
  payback_rate_percent: '',
  current_value_twd: '',
  fund_code: '',
  fund_isin: '',
  fund_share_class: '',
  fund_quote_symbol: '',
}

const EXCEL_MAPPING_FIELDS = [
  { key: 'symbol', label: '代號', required: true },
  { key: 'name', label: '名稱', required: false },
  { key: 'market', label: '市場', required: false },
  { key: 'asset_type', label: '資產類型', required: false },
  { key: 'quantity', label: '數量', required: true },
  { key: 'average_cost', label: '平均成本', required: false },
  { key: 'currency', label: '幣別', required: false },
  { key: 'principal_amount', label: '原始本金', required: false },
  { key: 'principal_currency', label: '本金幣別', required: false },
  { key: 'principal_twd', label: '本金 TWD', required: false },
] as const

const CONSOLIDATED_ROW_FIELDS = [
  { key: 'fund_start_row_number', label: '基金起始列' },
  { key: 'security_start_row_number', label: '證券起始列' },
  { key: 'data_end_row_number', label: '資料結束列' },
] as const

const CONSOLIDATED_COLUMN_FIELDS = [
  { key: 'fund_name_column_index', label: '基金名稱' },
  { key: 'tw_symbol_column_index', label: '台股代號' },
  { key: 'tw_name_column_index', label: '台股名稱' },
  { key: 'us_symbol_column_index', label: '美股代號' },
  { key: 'us_name_column_index', label: '美股名稱' },
  { key: 'quantity_column_index', label: '數量 / 單位數' },
  { key: 'cost_amount_column_index', label: '成本金額' },
  { key: 'price_column_index', label: '價格 / 淨值' },
  { key: 'current_value_column_index', label: '新台幣現值' },
  { key: 'dividend_amount_column_index', label: '配息金額' },
  { key: 'dividend_per_unit_column_index', label: '單位配息' },
  { key: 'monthly_dividend_column_index', label: '每月配息' },
  { key: 'annual_dividend_yield_column_index', label: '年化配息率' },
  { key: 'payback_rate_column_index', label: '回本率' },
] as const

const EXCEL_COLUMN_OPTIONS = Array.from({ length: 96 }, (_, index) => index)

type ExcelColumnMapping = Record<string, string>
type ExcelLayoutMode =
  | 'consolidated_report'
  | 'horizontal_matrix'
  | 'row_mapping'

type ExcelHorizontalSheetDraft = {
  enabled: boolean
  sheet_name: string
  category_label: string
  market: string
  asset_type: string
  currency: string
  header_row_number: string
  holding_row_number: string
  price_row_number: string
  quantity_row_number: string
  first_asset_column_index: string
  group_width: string
  holding_count: number
  sample_holdings: ExcelHorizontalSheetPreview['sample_holdings']
}

type ExcelConsolidatedDraft = {
  sheet_name: string
  fund_start_row_number: string
  security_start_row_number: string
  data_end_row_number: string
  fund_name_column_index: string
  tw_symbol_column_index: string
  tw_name_column_index: string
  us_symbol_column_index: string
  us_name_column_index: string
  quantity_column_index: string
  cost_amount_column_index: string
  price_column_index: string
  current_value_column_index: string
  dividend_amount_column_index: string
  dividend_per_unit_column_index: string
  monthly_dividend_column_index: string
  annual_dividend_yield_column_index: string
  payback_rate_column_index: string
  fund_market: string
  fund_asset_type: string
  fund_currency: string
  fund_principal_currency: string
  tw_market: string
  tw_asset_type: string
  tw_currency: string
  tw_principal_currency: string
  us_market: string
  us_asset_type: string
  us_currency: string
  us_principal_currency: string
  include_zero_quantity: boolean
}

const EMPTY_CONSOLIDATED_DRAFT: ExcelConsolidatedDraft = {
  sheet_name: '',
  fund_start_row_number: '3',
  security_start_row_number: '45',
  data_end_row_number: '264',
  fund_name_column_index: '2',
  tw_symbol_column_index: '1',
  tw_name_column_index: '2',
  us_symbol_column_index: '2',
  us_name_column_index: '1',
  quantity_column_index: '13',
  cost_amount_column_index: '14',
  price_column_index: '18',
  current_value_column_index: '21',
  dividend_amount_column_index: '3',
  dividend_per_unit_column_index: '4',
  monthly_dividend_column_index: '5',
  annual_dividend_yield_column_index: '6',
  payback_rate_column_index: '7',
  fund_market: 'FUND',
  fund_asset_type: 'FUND',
  fund_currency: 'TWD',
  fund_principal_currency: 'TWD',
  tw_market: 'TW',
  tw_asset_type: 'AUTO',
  tw_currency: 'TWD',
  tw_principal_currency: 'TWD',
  us_market: 'US',
  us_asset_type: 'AUTO',
  us_currency: 'USD',
  us_principal_currency: 'TWD',
  include_zero_quantity: true,
}

function excelColumnLetter(index: number): string {
  let value = index + 1
  let letters = ''
  while (value > 0) {
    value -= 1
    letters = String.fromCharCode(65 + (value % 26)) + letters
    value = Math.floor(value / 26)
  }
  return letters
}

function socketStatusLabel(status: string): string {
  if (status === 'Connected') return '已連線'
  if (status === 'Connecting') return '連線中'
  if (status === 'Disconnected') return '未連線'
  if (status === 'Error') return '連線錯誤'
  return status
}

function waitForIpcEvent<T = Record<string, unknown>>(
  eventName: string,
  timeoutMs: number,
  requestId: string
): Promise<T> {
  return new Promise((resolve, reject) => {
    let timer = 0
    const cleanup = () => {
      window.clearTimeout(timer)
      window.removeEventListener('ipc_event', handler)
      window.removeEventListener('socket_connected', connectionHandler)
    }
    const handler = (event: Event) => {
      const customEvent = event as CustomEvent
      const detail = customEvent.detail || {}
      const payload = (detail.payload || {}) as Record<string, unknown>
      if (detail.event !== eventName) return
      if (requestId && String(payload.request_id || '') !== requestId) return
      cleanup()
      resolve(payload as T)
    }
    const connectionHandler = (event: Event) => {
      const customEvent = event as CustomEvent
      if (customEvent.detail?.connected !== false) return
      cleanup()
      reject(
        new Error(
          '後端連線已中斷，操作結果未知；重新連線後請重新載入狀態'
        )
      )
    }
    timer = window.setTimeout(() => {
      cleanup()
      reject(new Error(`等待 ${eventName} 逾時`))
    }, timeoutMs)
    window.addEventListener('ipc_event', handler)
    window.addEventListener('socket_connected', connectionHandler)
  })
}

export function AiAssistantWindowApp() {
  const {
    sendCommand,
    status: socketStatus,
    waitUntilConnected,
  } = useLocalBackendSocket()
  const [message, setMessage] = useState('AI投資管家已就緒')
  const [busyAction, setBusyAction] = useState('')
  const [paths, setPaths] = useState({ workspace: '', state: '' })
  const [holdingEditorOpen, setHoldingEditorOpen] = useState(false)
  const [holdingDraft, setHoldingDraft] = useState<HoldingDraft>(
    EMPTY_HOLDING_DRAFT
  )
  const [excelMapperOpen, setExcelMapperOpen] = useState(false)
  const [excelMappingPreview, setExcelMappingPreview] =
    useState<ExcelMappingPreview | null>(null)
  const [excelSheetName, setExcelSheetName] = useState('')
  const [excelHeaderRow, setExcelHeaderRow] = useState(1)
  const [excelDataStartRow, setExcelDataStartRow] = useState(2)
  const [excelColumnMapping, setExcelColumnMapping] =
    useState<ExcelColumnMapping>({})
  const [excelLayoutMode, setExcelLayoutMode] =
    useState<ExcelLayoutMode>('row_mapping')
  const [excelHorizontalSheets, setExcelHorizontalSheets] = useState<
    ExcelHorizontalSheetDraft[]
  >([])
  const [excelConsolidatedDraft, setExcelConsolidatedDraft] =
    useState<ExcelConsolidatedDraft>(EMPTY_CONSOLIDATED_DRAFT)
  const autoMissingInfoKeyRef = useRef('')

  const request = useCallback(
    async (
      command: string,
      payload: Record<string, unknown> = {},
      timeoutMs = 30000
    ) => {
      if (command !== 'investment_watch_get_state') {
        await waitUntilConnected(15_000)
      }
      const requestId = `${command}:${Date.now()}:${Math.random().toString(16).slice(2)}`
      const waitPromise = waitForIpcEvent<Record<string, unknown>>(
        `${command}_result`,
        timeoutMs,
        requestId
      )
      const sent = sendCommand(command, { ...payload, request_id: requestId })
      if (!sent.ok && !sent.queued) {
        void waitPromise.catch(() => undefined)
        throw new Error(sent.message || '送出投資管家指令失敗')
      }
      const response = await waitPromise
      const shell = response as InvestmentShellState
      if (
        typeof shell.tool_root === 'string' ||
        typeof shell.state_path === 'string'
      ) {
        setPaths({
          workspace: String(shell.tool_root || ''),
          state: String(shell.state_path || ''),
        })
      }
      return response
    },
    [sendCommand, waitUntilConnected]
  )

  const investmentWatch = useInvestmentWatchFeature({
    request,
    setBusyAction,
    setMessage,
  })
  const { loadInvestmentState } = investmentWatch
  const portfolio = investmentWatch.investmentState.portfolio
  const visibleHoldings = investmentWatch.investmentHoldings
  const latestInvestmentRuns = investmentWatch.investmentRuns.slice(0, 8)
  const portfolioUpdatedAt =
    formatInvestmentClock(portfolio?.imported_at) || '未更新'
  const socketLabel = socketStatusLabel(socketStatus)
  const openMarkets =
    investmentWatch.investmentState.market_sessions?.open_markets || []
  const liveUpdateLabel =
    socketStatus === 'Connected'
      ? openMarkets.length > 0
        ? `即時 · ${openMarkets.join('/')} 開盤`
        : '即時 · 目前休市'
      : socketLabel
  const workbookScan = investmentWatch.investmentState.workbook_scan
  const workbookQuality = investmentWatch.investmentState.workbook_scan_quality
  const selectedWorkbookSheet = workbookScan?.selected_sheet || null
  const workbookScanLabel = selectedWorkbookSheet?.sheet_name
    ? selectedWorkbookSheet.header_row_number
      ? `${selectedWorkbookSheet.sheet_name} · 第 ${selectedWorkbookSheet.header_row_number} 列`
      : `${selectedWorkbookSheet.sheet_name} · 資料第 ${selectedWorkbookSheet.data_start_row_number || 1} 列`
    : workbookScan?.sheet_count
      ? `已掃描 ${workbookScan.sheet_count} 張工作表`
      : '尚未掃描'
  const excelImportProfile = investmentWatch.investmentState.excel_import_profile
  const portfolioVersions = investmentWatch.investmentState.portfolio_versions || []
  const latestPortfolioVersion = portfolioVersions[0]
  const selectedExcelSheet = useMemo<ExcelMappingSheetPreview | null>(
    () =>
      excelMappingPreview?.sheets.find(
        (sheet) => sheet.sheet_name === excelSheetName
      ) ||
      excelMappingPreview?.sheets[0] ||
      null,
    [excelMappingPreview, excelSheetName]
  )
  const excelHeaderValues = useMemo(() => {
    const values = selectedExcelSheet?.rows[excelHeaderRow - 1] || []
    const width = Math.max(selectedExcelSheet?.column_count || 0, values.length)
    return Array.from({ length: width }, (_, index) => values[index] || '')
  }, [selectedExcelSheet, excelHeaderRow])
  const mappedExcelPreviewRows = useMemo(() => {
    if (!selectedExcelSheet) return []
    return selectedExcelSheet.rows
      .slice(Math.max(0, excelDataStartRow - 1))
      .filter((row) =>
        EXCEL_MAPPING_FIELDS.some(({ key }) => {
          const rawIndex = excelColumnMapping[key]
          if (rawIndex === undefined || rawIndex === '') return false
          return String(row[Number(rawIndex)] || '').trim().length > 0
        })
      )
      .slice(0, 6)
  }, [selectedExcelSheet, excelDataStartRow, excelColumnMapping])
  const selectedHorizontalSheets = useMemo(
    () => excelHorizontalSheets.filter((sheet) => sheet.enabled),
    [excelHorizontalSheets]
  )
  const horizontalHoldingCount = useMemo(
    () =>
      selectedHorizontalSheets.reduce(
        (total, sheet) => total + sheet.holding_count,
        0
      ),
    [selectedHorizontalSheets]
  )
  const horizontalSampleHoldings = useMemo(
    () =>
      selectedHorizontalSheets
        .flatMap((sheet) =>
          (sheet.sample_holdings || []).map((holding) => ({
            ...holding,
            source_sheet: sheet.sheet_name,
          }))
        )
        .slice(0, 10),
    [selectedHorizontalSheets]
  )
  const localAiStatus = investmentWatch.localAiProductStatus
  const diagnostics = investmentWatch.investmentDiagnostics
  const localAiStatusLabel =
    localAiStatus?.state_label ||
    (investmentWatch.investmentHoldings.length > 0
      ? '等待星澄分析'
      : '等待持股資料')
  const localAiScore =
    typeof localAiStatus?.score === 'number' ? String(localAiStatus.score) : '-'
  const localAiActions = (localAiStatus?.next_actions || [])
    .filter(Boolean)
    .slice(0, 4)
  const localAiCommands = (localAiStatus?.command_suggestions || [])
    .filter(Boolean)
    .slice(0, 6)
  const localAiWarnings = investmentWatch.localAiRiskWarnings.slice(0, 8)
  const localAiCommandResult = investmentWatch.localAiCommandResult
  const localAiCommandPreview = (localAiCommandResult?.text || '').trim()
  const localAiActionPlan = (
    localAiCommandResult?.action_plan ||
    investmentWatch.investmentState.local_ai_action_plan ||
    []
  ).slice(0, 5)
  const localAiWatchTriggers = (
    localAiCommandResult?.watch_triggers ||
    investmentWatch.investmentState.local_ai_watch_triggers ||
    []
  ).slice(0, 6)
  const localAiConfidence =
    localAiCommandResult?.confidence_summary ||
    investmentWatch.investmentState.local_ai_confidence ||
    null
  const localAiDecisionBrief =
    localAiCommandResult?.decision_brief ||
    investmentWatch.investmentState.local_ai_decision_brief ||
    localAiStatus?.decision_summary ||
    ''
  const localAiNetworkContext =
    localAiCommandResult?.network_context ||
    investmentWatch.investmentState.local_ai_network_context ||
    localAiStatus?.network_context ||
    null
  const localAiExplanation = investmentWatch.investmentState.local_ai_explanation
  const localAiSourceConfidence = localAiStatus?.source_confidence
  const holdingQuoteStatus = useMemo(() => {
    const eligible = visibleHoldings.filter(
      (holding) =>
        Number(holding.quantity || 0) > 0 &&
        ['TW', 'US', 'HK'].includes(String(holding.market || '').toUpperCase())
    )
    const quoted = eligible.filter(
      (holding) => Number(holding.web_current_price || 0) > 0
    )
    const providers = new Set(
      quoted
        .map((holding) => String(holding.market_data_source || '').trim())
        .filter(Boolean)
    )
    return {
      eligibleCount: eligible.length,
      quotedCount: quoted.length,
      providerCount: providers.size,
      coveragePercent:
        eligible.length > 0 ? (quoted.length / eligible.length) * 100 : 0,
    }
  }, [visibleHoldings])
  const holdingQuoteCoverage =
    holdingQuoteStatus.eligibleCount > 0
      ? `${holdingQuoteStatus.quotedCount} / ${holdingQuoteStatus.eligibleCount} 筆（${formatInvestmentNumber(holdingQuoteStatus.coveragePercent, 1)}%）`
      : '尚無可報價持股'
  const holdingQuoteAvailable = holdingQuoteStatus.quotedCount > 0
  const localAiNetworkLabel =
    localAiNetworkContext?.mode_label ||
    localAiStatus?.network_mode_label ||
    diagnostics?.local_ai?.network_mode_label ||
    localAiStatus?.watch_status_label ||
    (holdingQuoteAvailable
      ? openMarkets.length > 0
        ? '盤中自動報價'
        : '收盤報價可用'
      : openMarkets.length > 0
        ? '開盤同步中'
        : '目前休市')
  const localAiNetworkCoverage =
    localAiNetworkContext?.coverage_label ||
    localAiStatus?.coverage_label ||
    diagnostics?.local_ai?.coverage_label ||
    holdingQuoteCoverage
  const localAiQuoteHealth =
    localAiNetworkContext?.health ||
    localAiStatus?.quote_health ||
    diagnostics?.local_ai?.quote_health ||
    (holdingQuoteAvailable ? 'ready' : openMarkets.length > 0 ? 'attention' : 'offline')
  const localAiQuoteHealthLabel =
    localAiNetworkContext?.health_label ||
    localAiStatus?.quote_health_label ||
    diagnostics?.local_ai?.quote_health_label ||
    (holdingQuoteAvailable
      ? '報價可用'
      : openMarkets.length > 0
        ? '同步中'
        : '等待開盤')
  const localAiProviderCount =
    localAiNetworkContext?.quote_provider_count ??
    diagnostics?.local_ai?.quote_provider_count ??
    holdingQuoteStatus.providerCount
  const localAiQuoteGaps = (
    localAiNetworkContext?.quote_gaps ||
    diagnostics?.local_ai?.quote_gaps ||
    []
  ).slice(0, 8)
  const localAiQuoteGapCount =
    localAiNetworkContext?.quote_gap_count ??
    diagnostics?.local_ai?.quote_gap_count ??
    localAiQuoteGaps.length
  const localAiCrossCheckedCount =
    localAiNetworkContext?.cross_checked_count ??
    diagnostics?.local_ai?.cross_checked_count ??
    0
  const localAiUntrustedQuoteCount =
    localAiNetworkContext?.untrusted_quote_count ??
    diagnostics?.local_ai?.untrusted_quote_count ??
    0
  const localAiVerificationLabel = `驗證 ${localAiCrossCheckedCount} · 異常 ${localAiUntrustedQuoteCount}`
  const localAiRecommendation =
    localAiStatus?.recommendation ||
    (investmentWatch.investmentHoldings.length > 0
      ? '星澄會依照持倉成本、報價、集中度與動態權重產生風險預告。'
      : '讀取 Excel 持股檔後，星澄會自動執行監測。')
  const holdingValueLabel = useMemo(() => {
    const count = investmentWatch.investmentHoldings.length
    return `${count} 筆持股`
  }, [investmentWatch.investmentHoldings.length])
  const estimatedWeeklyDividendTwd = useMemo(
    () =>
      investmentWatch.investmentHoldings.reduce(
        (total, holding) =>
          total + Number(holding.estimated_weekly_dividend_twd || 0),
        0
      ),
    [investmentWatch.investmentHoldings]
  )
  const diagnosticState = diagnostics?.state || 'setup'
  const diagnosticStateLabel =
    diagnostics?.state_label ||
    (investmentWatch.investmentHoldings.length > 0
      ? '診斷同步中'
      : '等待持股資料')
  const diagnosticMessage =
    diagnostics?.message ||
    (investmentWatch.investmentHoldings.length > 0
      ? '已載入持股資料，正在同步本地診斷。'
      : '請先讀取持股檔，診斷會在載入後自動更新。')
  const portfolioAgeHours = diagnostics?.portfolio?.age_hours
  const portfolioAgeLabel =
    typeof portfolioAgeHours === 'number'
      ? `${formatInvestmentNumber(portfolioAgeHours, 1)} 小時`
      : '-'
  const workbookDiagnosticLabel =
    diagnostics?.workbook?.state_label ||
    workbookQuality?.state_label ||
    '尚未掃描'
  const latestRunLabel = diagnostics?.runs?.latest?.created_at
    ? formatInvestmentClock(diagnostics.runs.latest.created_at)
    : '尚未執行'
  const applyLocalCommandPreset = useCallback(
    (command: string) => {
      investmentWatch.setLocalRiskCommand(command)
    },
    [investmentWatch.setLocalRiskCommand]
  )
  const mobileSync = investmentWatch.mobileSync
  const [mobileSyncRemoteDraft, setMobileSyncRemoteDraft] = useState('')
  const mobileSyncRemoteUrl = mobileSync?.remote_url || ''
  const mobileSyncLocalUrl =
    mobileSync?.local_urls?.[0] || mobileSync?.loopback_url || ''
  const mobileSyncPhoneUrl = mobileSyncRemoteUrl || mobileSyncLocalUrl || ''
  const mobileSyncPairingCode = mobileSync?.pairing_code || '--------'
  const mobileSyncStatusLabel =
    mobileSync?.remote_status_label ||
    (mobileSync?.running ? '本機/區網' : '同步服務準備中')

  useEffect(() => {
    setMobileSyncRemoteDraft(mobileSync?.remote_base_url || '')
  }, [mobileSync?.remote_base_url])

  useEffect(() => {
    if (socketStatus !== 'Connected') return undefined
    const refreshState = () => {
      void loadInvestmentState(true)
    }
    const refreshMarketQuotes = () => {
      void investmentWatch.syncOpenMarkets()
    }
    const refreshNow = () => {
      refreshState()
      refreshMarketQuotes()
    }
    const handleVisibility = () => {
      if (document.visibilityState === 'visible') refreshNow()
    }

    void loadInvestmentState(false, true)
    refreshMarketQuotes()
    const stateTimer = window.setInterval(refreshState, 5000)
    const marketTimer = window.setInterval(refreshMarketQuotes, 60000)
    window.addEventListener('focus', refreshNow)
    window.addEventListener('online', refreshNow)
    document.addEventListener('visibilitychange', handleVisibility)
    return () => {
      window.clearInterval(stateTimer)
      window.clearInterval(marketTimer)
      window.removeEventListener('focus', refreshNow)
      window.removeEventListener('online', refreshNow)
      document.removeEventListener('visibilitychange', handleVisibility)
    }
  }, [investmentWatch.syncOpenMarkets, loadInvestmentState, socketStatus])

  useEffect(() => {
    if (socketStatus !== 'Connected' || visibleHoldings.length === 0) return
    const activeTradable = visibleHoldings.filter(
      (holding) =>
        Number(holding.quantity || 0) > 0 &&
        ['TW', 'US', 'HK'].includes(String(holding.market || '').toUpperCase())
    )
    const syncInfo = investmentWatch.investmentState.dividend_sync
    if (
      String(syncInfo?.portfolio_imported_at || '') ===
        String(portfolio?.imported_at || '') &&
      Number(syncInfo?.portfolio_manual_revision || 0) ===
        Number(portfolio?.manual_revision || 0)
    ) {
      return
    }
    const hasMissingInfo = activeTradable.some(
      (holding) =>
        !holding.market_data_updated_at ||
        !holding.dividend_frequency ||
        !holding.currency ||
        !holding.asset_type ||
        holding.asset_type === 'AUTO' ||
        !holding.name ||
        holding.name.toUpperCase() === String(holding.symbol || '').toUpperCase()
    )
    if (!hasMissingInfo) return
    const key = [
      portfolio?.imported_at || '',
      portfolio?.manual_revision || 0,
      activeTradable.length,
    ].join(':')
    if (!key || autoMissingInfoKeyRef.current === key) return
    autoMissingInfoKeyRef.current = key
    void investmentWatch.syncMissingInfo()
  }, [
    investmentWatch.syncMissingInfo,
    investmentWatch.investmentState.dividend_sync,
    portfolio?.imported_at,
    portfolio?.manual_revision,
    socketStatus,
    visibleHoldings,
  ])

  const openNewHolding = () => {
    setHoldingDraft(EMPTY_HOLDING_DRAFT)
    setExcelMapperOpen(false)
    setHoldingEditorOpen(true)
  }

  const openHoldingEditor = (holding: InvestmentHolding) => {
    setHoldingDraft({
      holding_id: holding.holding_id || '',
      symbol: holding.symbol || '',
      name: holding.name || '',
      market: holding.market || 'TW',
      asset_type: holding.asset_type || 'STOCK',
      quantity: String(holding.quantity ?? 1),
      average_cost: String(holding.average_cost ?? 0),
      currency: holding.currency || 'TWD',
      principal_amount: String(holding.principal_amount ?? ''),
      principal_currency:
        holding.principal_currency || holding.currency || 'TWD',
      principal_twd: String(holding.principal_twd ?? ''),
      dividend_amount_twd: String(holding.dividend_amount_twd ?? ''),
      dividend_per_unit: String(holding.dividend_per_unit ?? ''),
      monthly_dividend_twd: String(holding.monthly_dividend_twd ?? ''),
      annual_dividend_yield_percent: String(
        holding.annual_dividend_yield_percent ?? ''
      ),
      payback_rate_percent: String(holding.payback_rate_percent ?? ''),
      current_value_twd: String(holding.current_value_twd ?? ''),
      fund_code: holding.fund_code || '',
      fund_isin: holding.fund_isin || '',
      fund_share_class: holding.fund_share_class || '',
      fund_quote_symbol: holding.fund_quote_symbol || '',
    })
    setExcelMapperOpen(false)
    setHoldingEditorOpen(true)
  }

  const saveHolding = async (event: React.FormEvent) => {
    event.preventDefault()
    const result = await investmentWatch.runInvestmentV2Command(
      'investment_watch_upsert_holding',
      {
        ...holdingDraft,
        quantity: Number(holdingDraft.quantity),
        average_cost: Number(holdingDraft.average_cost),
      },
      holdingDraft.holding_id ? '持股修改' : '持股新增'
    )
    if (result) {
      setHoldingEditorOpen(false)
      setHoldingDraft(EMPTY_HOLDING_DRAFT)
    }
  }

  const deleteHolding = async (holding: InvestmentHolding) => {
    if (!holding.holding_id) {
      setMessage('請先重新匯入或編輯此持股，以建立本機識別碼。')
      return
    }
    if (!window.confirm(`確定刪除 ${holding.symbol || '這筆持股'}？原始 Excel 不會被修改。`)) return
    const result = await investmentWatch.runInvestmentV2Command(
      'investment_watch_delete_holding',
      { holding_id: holding.holding_id, confirmed: true },
      '持股刪除'
    )
    if (result && holdingDraft.holding_id === holding.holding_id) {
      setHoldingEditorOpen(false)
      setHoldingDraft(EMPTY_HOLDING_DRAFT)
    }
  }

  const restoreLatestPortfolioVersion = async () => {
    if (!latestPortfolioVersion?.version_id) return
    if (
      !window.confirm(
        `確定還原 ${latestPortfolioVersion.holding_count || 0} 筆持股版本？目前版本會先自動保留。`
      )
    )
      return
    await investmentWatch.runInvestmentV2Command(
      'investment_watch_restore_portfolio_version',
      { version_id: latestPortfolioVersion.version_id, confirmed: true },
      '持股版本還原',
      120000
    )
  }

  const resolveFundIdentities = async () => {
    await investmentWatch.runInvestmentV2Command(
      'investment_watch_resolve_fund_identities',
      { limit: 80 },
      '共同基金辨識',
      240000,
      true
    )
  }

  const initializeConsolidatedReport = (preview: ExcelMappingPreview) => {
    const detected = preview.consolidated_layout
    if (!detected?.detected) {
      setExcelConsolidatedDraft(EMPTY_CONSOLIDATED_DRAFT)
      return
    }
    const saved =
      excelImportProfile?.source_path === preview.source_path &&
      excelImportProfile?.layout === 'consolidated_report'
        ? (excelImportProfile as Record<string, unknown>)
        : null
    const detectedValues = detected as Record<string, unknown>
    const next = { ...EMPTY_CONSOLIDATED_DRAFT }
    for (const key of Object.keys(next) as Array<keyof ExcelConsolidatedDraft>) {
      const value = saved?.[key] ?? detectedValues[key] ?? next[key]
      if (key === 'include_zero_quantity') {
        next[key] = Boolean(value) as never
      } else {
        next[key] = String(value ?? '') as never
      }
    }
    setExcelConsolidatedDraft(next)
  }

  const initializeHorizontalSheets = (preview: ExcelMappingPreview) => {
    const detected = preview.horizontal_layout?.sheets || []
    const savedSheets =
      excelImportProfile?.source_path === preview.source_path &&
      excelImportProfile?.layout === 'horizontal_matrix' &&
      Array.isArray(excelImportProfile.sheets)
        ? excelImportProfile.sheets
        : []
    setExcelHorizontalSheets(
      detected.map((sheet) => {
        const saved = savedSheets.find(
          (item) => String(item.sheet_name || '') === sheet.sheet_name
        )
        const value = (key: string, fallback: unknown) =>
          String(saved?.[key] ?? fallback ?? '')
        return {
          enabled:
            savedSheets.length > 0
              ? Boolean(saved)
              : Boolean(sheet.selected_by_default),
          sheet_name: sheet.sheet_name,
          category_label: value('category_label', sheet.category_label),
          market: value('market', sheet.market || 'TW'),
          asset_type: value('asset_type', sheet.asset_type || 'AUTO'),
          currency: value('currency', sheet.currency || 'TWD'),
          header_row_number: value(
            'header_row_number',
            sheet.header_row_number || 1
          ),
          holding_row_number: value(
            'holding_row_number',
            sheet.holding_row_number || 1
          ),
          price_row_number: value(
            'price_row_number',
            sheet.price_row_number ?? ''
          ),
          quantity_row_number: value(
            'quantity_row_number',
            sheet.quantity_row_number || 1
          ),
          first_asset_column_index: value(
            'first_asset_column_index',
            sheet.first_asset_column_index ?? 1
          ),
          group_width: value('group_width', sheet.group_width || 4),
          holding_count: sheet.holding_count || 0,
          sample_holdings: sheet.sample_holdings || [],
        }
      })
    )
  }

  const updateHorizontalSheet = (
    sheetName: string,
    changes: Partial<ExcelHorizontalSheetDraft>
  ) => {
    setExcelHorizontalSheets((current) =>
      current.map((sheet) =>
        sheet.sheet_name === sheetName ? { ...sheet, ...changes } : sheet
      )
    )
  }

  const mappingForExcelSheet = (
    preview: ExcelMappingPreview,
    sheet: ExcelMappingSheetPreview,
    preferSaved = true
  ): ExcelColumnMapping => {
    const savedMatches =
      preferSaved &&
      excelImportProfile?.source_path === preview.source_path &&
      excelImportProfile?.sheet_name === sheet.sheet_name
    const sourceMapping = savedMatches
      ? excelImportProfile?.column_mapping
      : sheet.suggested_mapping
    return Object.fromEntries(
      Object.entries(sourceMapping || {}).map(([field, index]) => [
        field,
        String(index),
      ])
    )
  }

  const selectExcelSheet = (
    preview: ExcelMappingPreview,
    sheet: ExcelMappingSheetPreview,
    preferSaved = true
  ) => {
    const savedMatches =
      preferSaved &&
      excelImportProfile?.source_path === preview.source_path &&
      excelImportProfile?.sheet_name === sheet.sheet_name
    setExcelSheetName(sheet.sheet_name)
    setExcelHeaderRow(
      savedMatches
        ? excelImportProfile?.header_row_number || 1
        : sheet.suggested_header_row_number || 1
    )
    setExcelDataStartRow(
      savedMatches
        ? excelImportProfile?.data_start_row_number || 2
        : sheet.suggested_data_start_row_number ||
            (sheet.suggested_header_row_number || 1) + 1
    )
    setExcelColumnMapping(mappingForExcelSheet(preview, sheet, preferSaved))
  }

  const applySmartExcelRepair = () => {
    if (!excelMappingPreview?.smart_repair?.recommended) return
    const repair = excelMappingPreview.smart_repair
    if (repair.layout === 'consolidated_report') {
      setExcelLayoutMode('consolidated_report')
      initializeConsolidatedReport(excelMappingPreview)
      return
    }
    if (repair.layout === 'horizontal_matrix') {
      setExcelLayoutMode('horizontal_matrix')
      initializeHorizontalSheets(excelMappingPreview)
      return
    }
    const sheet =
      excelMappingPreview.sheets.find(
        (item) => item.sheet_name === repair.sheet_name
      ) || excelMappingPreview.sheets[0]
    if (!sheet) return
    setExcelLayoutMode('row_mapping')
    setExcelSheetName(sheet.sheet_name)
    setExcelHeaderRow(repair.header_row_number || 1)
    setExcelDataStartRow(
      repair.data_start_row_number || (repair.header_row_number || 1) + 1
    )
    setExcelColumnMapping(
      Object.fromEntries(
        Object.entries(repair.column_mapping || {}).map(([field, index]) => [
          field,
          String(index),
        ])
      )
    )
  }

  const openExcelMapper = async (chooseFile = false) => {
    let sourcePath = chooseFile ? '' : portfolio?.source_path || ''
    if (!sourcePath.toLowerCase().endsWith('.xlsx')) {
      sourcePath = await openInvestmentFile()
    }
    if (!sourcePath) {
      setMessage('未選擇 Excel 檔案。')
      return
    }
    if (!sourcePath.toLowerCase().endsWith('.xlsx')) {
      setMessage('欄位設定目前支援 .xlsx，請先將舊版 Excel 另存為 .xlsx。')
      return
    }
    const result = await investmentWatch.runInvestmentV2Command(
      'investment_watch_preview_excel_mapping',
      { path: sourcePath },
      'Excel 欄位預覽',
      120000
    )
    const preview = result?.excel_mapping_preview as
      | ExcelMappingPreview
      | undefined
    if (!preview?.sheets.length) return
    setExcelMappingPreview(preview)
    initializeConsolidatedReport(preview)
    initializeHorizontalSheets(preview)
    setExcelLayoutMode(
      excelImportProfile?.source_path === preview.source_path &&
        excelImportProfile?.layout === 'consolidated_report'
        ? 'consolidated_report'
        : excelImportProfile?.source_path === preview.source_path &&
            excelImportProfile?.layout === 'horizontal_matrix'
          ? 'horizontal_matrix'
          : preview.consolidated_layout?.recommended
            ? 'consolidated_report'
            : preview.horizontal_layout?.recommended
              ? 'horizontal_matrix'
              : 'row_mapping'
    )
    const savedSheet =
      excelImportProfile?.source_path === preview.source_path
        ? preview.sheets.find(
            (sheet) => sheet.sheet_name === excelImportProfile.sheet_name
          )
        : undefined
    const initialSheet =
      savedSheet ||
      preview.sheets.find(
        (sheet) => sheet.sheet_name === preview.selected_sheet_name
      ) ||
      preview.sheets[0]
    selectExcelSheet(preview, initialSheet, true)
    setHoldingEditorOpen(false)
    setExcelMapperOpen(true)
  }

  const importExcelWithMapping = async (event: React.FormEvent) => {
    event.preventDefault()
    if (!excelMappingPreview || !selectedExcelSheet) return
    if (excelLayoutMode === 'consolidated_report') {
      const numericFields: Array<keyof ExcelConsolidatedDraft> = [
        'fund_start_row_number',
        'security_start_row_number',
        'data_end_row_number',
        'fund_name_column_index',
        'tw_symbol_column_index',
        'tw_name_column_index',
        'us_symbol_column_index',
        'us_name_column_index',
        'quantity_column_index',
        'cost_amount_column_index',
        'price_column_index',
        'current_value_column_index',
        'dividend_amount_column_index',
        'dividend_per_unit_column_index',
        'monthly_dividend_column_index',
        'annual_dividend_yield_column_index',
        'payback_rate_column_index',
      ]
      const config: Record<string, unknown> = { ...excelConsolidatedDraft }
      for (const field of numericFields) {
        config[field] = Number(excelConsolidatedDraft[field])
      }
      const result = await investmentWatch.runInvestmentV2Command(
        'investment_watch_import_excel_mapping',
        {
          path: excelMappingPreview.source_path,
          layout: 'consolidated_report',
          config,
        },
        '報酬工作表匯入',
        240000
      )
      if (result) {
        setExcelMapperOpen(false)
        setExcelMappingPreview(null)
      }
      return
    }
    if (excelLayoutMode === 'horizontal_matrix') {
      if (selectedHorizontalSheets.length === 0) {
        setMessage('請至少選擇一張 ETF、台股、美股或共同基金工作表。')
        return
      }
      const invalidSheet = selectedHorizontalSheets.find(
        (sheet) =>
          !sheet.header_row_number ||
          !sheet.holding_row_number ||
          !sheet.quantity_row_number ||
          !sheet.group_width
      )
      if (invalidSheet) {
        setMessage(`請完成 ${invalidSheet.sheet_name} 的列與欄位設定。`)
        return
      }
      const result = await investmentWatch.runInvestmentV2Command(
        'investment_watch_import_excel_mapping',
        {
          path: excelMappingPreview.source_path,
          layout: 'horizontal_matrix',
          sheets: selectedHorizontalSheets.map((sheet) => ({
            enabled: true,
            sheet_name: sheet.sheet_name,
            category_label: sheet.category_label,
            market: sheet.market,
            asset_type: sheet.asset_type,
            currency: sheet.currency,
            header_row_number: Number(sheet.header_row_number),
            holding_row_number: Number(sheet.holding_row_number),
            price_row_number:
              sheet.price_row_number === ''
                ? null
                : Number(sheet.price_row_number),
            quantity_row_number: Number(sheet.quantity_row_number),
            first_asset_column_index: Number(
              sheet.first_asset_column_index
            ),
            group_width: Number(sheet.group_width),
          })),
        },
        'Excel 橫向持股匯入',
        240000
      )
      if (result) {
        setExcelMapperOpen(false)
        setExcelMappingPreview(null)
      }
      return
    }
    const missing = EXCEL_MAPPING_FIELDS.filter(
      (field) =>
        field.required &&
        (excelColumnMapping[field.key] === undefined ||
          excelColumnMapping[field.key] === '')
    )
    if (missing.length > 0) {
      setMessage(`請設定必要欄位：${missing.map((field) => field.label).join('、')}`)
      return
    }
    const columnMapping = Object.fromEntries(
      Object.entries(excelColumnMapping)
        .filter(([, index]) => index !== '')
        .map(([field, index]) => [field, Number(index)])
    )
    const result = await investmentWatch.runInvestmentV2Command(
      'investment_watch_import_excel_mapping',
      {
        path: excelMappingPreview.source_path,
        sheet_name: selectedExcelSheet.sheet_name,
        header_row_number: excelHeaderRow,
        data_start_row_number: excelDataStartRow,
        column_mapping: columnMapping,
      },
      'Excel 欄位匯入',
      240000
    )
    if (result) {
      setExcelMapperOpen(false)
      setExcelMappingPreview(null)
    }
  }

  return (
    <main className="nexus-app" data-update-mode="direct-overlay">
      <header className="nexus-topbar">
        <div className="nexus-title-block">
          <p className="nexus-eyebrow">投資管理</p>
          <h1>AI投資管家</h1>
          <div className="nexus-top-meta">
            <span>{liveUpdateLabel}</span>
            <span>星澄分析 · 外部 AI 討論</span>
            <span>{holdingValueLabel}</span>
            <span>
              週配息 NT$ {formatInvestmentNumber(estimatedWeeklyDividendTwd, 2)}
            </span>
          </div>
        </div>
        <div className="nexus-toolbar" aria-label="主要操作">
          <button
            type="button"
            className="nexus-primary"
            onClick={() => void investmentWatch.readPortfolioFile()}
            disabled={Boolean(busyAction)}
          >
            {busyAction === 'investment:read-file' ? '讀取中...' : '讀取檔案'}
          </button>
          <button
            type="button"
            onClick={() => void openExcelMapper(false)}
            disabled={Boolean(busyAction)}
          >
            {busyAction ===
            'investment:investment_watch_preview_excel_mapping'
              ? '預覽中...'
              : 'Excel 欄位'}
          </button>
          <button
            type="button"
            onClick={() => void loadInvestmentState()}
            disabled={Boolean(busyAction)}
          >
            重新整理
          </button>
          <button
            type="button"
            title="還原最近一次匯入或手動修改前的持股"
            onClick={() => void restoreLatestPortfolioVersion()}
            disabled={Boolean(busyAction) || !latestPortfolioVersion}
          >
            還原持股
          </button>
          <button
            type="button"
            title="預設匯出不含持股明細、路徑與帳務金額的去識別支援報告"
            onClick={() => void investmentWatch.exportInvestmentReport()}
            disabled={Boolean(busyAction)}
          >
            {busyAction === 'investment:export-report'
              ? '匯出中...'
              : '匯出去識別診斷'}
          </button>
          <button
            type="button"
            className="nexus-danger"
            onClick={() => void investmentWatch.clearInvestmentData()}
            disabled={Boolean(busyAction)}
          >
            {busyAction === 'investment:clear' ? '清除中...' : '刪除舊資料'}
          </button>
        </div>
      </header>

      <section className="nexus-status-strip" aria-label="投資管家狀態">
        <div>
          <span>投資檔案</span>
          <strong>{portfolio?.file_name || '尚未匯入'}</strong>
        </div>
        <div>
          <span>工作表掃描</span>
          <strong>{workbookScanLabel}</strong>
        </div>
        <div>
          <span>更新時間</span>
          <strong>{portfolioUpdatedAt}</strong>
        </div>
        <div>
          <span>持股資料</span>
          <strong>{holdingValueLabel}</strong>
        </div>
        <div>
          <span>星澄</span>
          <strong>
            {localAiStatusLabel} · {localAiScore}
          </strong>
        </div>
        <div>
          <span>報價資料</span>
          <strong>
            {localAiQuoteHealthLabel} · {localAiNetworkCoverage}
          </strong>
        </div>
        <div>
          <span>報價驗證</span>
          <strong>{localAiVerificationLabel}</strong>
        </div>
        <div className="nexus-status-message">
          <span>訊息</span>
          <strong>{message}</strong>
        </div>
      </section>

      <section
        className={`nexus-diagnostics-panel nexus-diagnostics-panel--${diagnosticState}`}
        aria-label="本地診斷"
      >
        <div className="nexus-diagnostics-summary">
          <span>{diagnosticStateLabel}</span>
          <strong>{diagnosticMessage}</strong>
        </div>
        <div className="nexus-diagnostic-grid">
          <div>
            <span>資料年齡</span>
            <strong>{portfolioAgeLabel}</strong>
          </div>
          <div>
            <span>Excel 掃描</span>
            <strong>{workbookDiagnosticLabel}</strong>
          </div>
          <div>
            <span>星澄風險</span>
            <strong>
              {diagnostics?.local_ai?.warning_count ?? 0} /{' '}
              {diagnostics?.local_ai?.critical_count ?? 0}
            </strong>
          </div>
          <div>
            <span>網路報價</span>
            <strong>{localAiNetworkCoverage}</strong>
          </div>
          <div>
            <span>交叉驗證</span>
            <strong>{localAiVerificationLabel}</strong>
          </div>
          <div>
            <span>最近執行</span>
            <strong>{latestRunLabel}</strong>
          </div>
          <div>
            <span>本地錯誤記錄</span>
            <strong>{diagnostics?.error_logging?.count ?? 0} 筆</strong>
          </div>
        </div>
      </section>

      <InvestmentWorkbenchV2
        analytics={investmentWatch.investmentState.analytics || null}
        v3={investmentWatch.investmentState.v3 || null}
        portfolio={investmentWatch.investmentState.portfolio || null}
        holdings={investmentWatch.investmentHoldings}
        mobileSync={investmentWatch.mobileSync}
        busyAction={busyAction}
        runCommand={investmentWatch.runInvestmentV2Command}
      />

      <section className="nexus-workbench nexus-workbench--local">
        <section className="nexus-column nexus-column--main">
          <section className="nexus-surface nexus-holdings-surface">
            <div className="nexus-section-head">
              <span>持股資料</span>
              <div className="nexus-holding-head-actions">
                <strong>{investmentWatch.investmentHoldings.length}</strong>
                <button
                  type="button"
                  className="nexus-toggle-button"
                  onClick={() => void openExcelMapper(false)}
                  disabled={Boolean(busyAction)}
                >
                  欄位設定
                </button>
                <button
                  type="button"
                  className="nexus-toggle-button"
                  onClick={() =>
                    void investmentWatch.runInvestmentV2Command(
                      'investment_watch_sync_dividends',
                      {},
                      '星澄配息搜尋',
                      240000
                    )
                  }
                  disabled={Boolean(busyAction)}
                >
                  {busyAction ===
                  'investment:investment_watch_sync_dividends'
                    ? '同步中...'
                    : '星澄搜尋配息'}
                </button>
                <button
                  type="button"
                  className="nexus-toggle-button"
                  title="依基金名稱搜尋基金代號、級別與可用報價代號"
                  onClick={() => void resolveFundIdentities()}
                  disabled={Boolean(busyAction)}
                >
                  {busyAction ===
                  'investment:investment_watch_resolve_fund_identities'
                    ? '辨識中...'
                    : '辨識基金'}
                </button>
                <button
                  type="button"
                  className="nexus-toggle-button"
                  onClick={openNewHolding}
                  disabled={Boolean(busyAction)}
                >
                  新增持股
                </button>
              </div>
            </div>
            <p className="nexus-scan-note">
              {workbookScanLabel}
              {selectedWorkbookSheet?.header_mode
                ? ` · ${selectedWorkbookSheet.header_mode}`
                : ''}
            </p>
            {workbookQuality ? (
              <div className="nexus-scan-quality">
                <span>
                  掃描品質
                  <strong>{workbookQuality.state_label || '-'}</strong>
                </span>
                <span>
                  分數
                  <strong>{workbookQuality.score ?? '-'}</strong>
                </span>
                <span>
                  有效資料
                  <strong>{workbookQuality.valid_data_row_count ?? '-'}</strong>
                </span>
                <span>
                  表頭深度
                  <strong>{workbookQuality.header_depth || 1} 層</strong>
                </span>
                <p>{workbookQuality.recommendation}</p>
              </div>
            ) : null}
            {excelMapperOpen && excelMappingPreview && selectedExcelSheet ? (
              <form
                className="nexus-excel-mapper"
                onSubmit={(event) => void importExcelWithMapping(event)}
              >
                <div className="nexus-excel-mapper-head">
                  <div>
                    <strong>Excel 欄位設定</strong>
                    <span>{excelMappingPreview.file_name}</span>
                  </div>
                  <span className="nexus-readonly-state">來源唯讀</span>
                </div>
                {excelMappingPreview.smart_repair ? (
                  <div className="nexus-smart-repair">
                    <div>
                      <strong>
                        智慧修復 · {excelMappingPreview.smart_repair.confidence_label || '低'}
                        {' '}{excelMappingPreview.smart_repair.confidence_score || 0}%
                      </strong>
                      <span>{excelMappingPreview.smart_repair.reason}</span>
                    </div>
                    <button
                      type="button"
                      onClick={applySmartExcelRepair}
                      disabled={!excelMappingPreview.smart_repair.recommended}
                    >
                      套用建議
                    </button>
                  </div>
                ) : null}
                <div className="nexus-excel-layout-tabs" role="tablist">
                  {excelMappingPreview.consolidated_layout?.detected ? (
                    <button
                      type="button"
                      className={
                        excelLayoutMode === 'consolidated_report'
                          ? 'is-active'
                          : ''
                      }
                      onClick={() => setExcelLayoutMode('consolidated_report')}
                    >
                      報酬總表 · {excelMappingPreview.consolidated_layout.holding_count || 0}
                    </button>
                  ) : null}
                  {excelMappingPreview.horizontal_layout?.detected ? (
                    <button
                      type="button"
                      className={
                        excelLayoutMode === 'horizontal_matrix'
                          ? 'is-active'
                          : ''
                      }
                      onClick={() => setExcelLayoutMode('horizontal_matrix')}
                    >
                      多工作表
                    </button>
                  ) : null}
                  <button
                    type="button"
                    className={
                      excelLayoutMode === 'row_mapping' ? 'is-active' : ''
                    }
                    onClick={() => setExcelLayoutMode('row_mapping')}
                  >
                    一般欄位
                  </button>
                </div>
                {excelLayoutMode === 'consolidated_report' &&
                excelMappingPreview.consolidated_layout ? (
                  <>
                    <div className="nexus-consolidated-summary">
                      <span>
                        全部資料
                        <strong>
                          {excelMappingPreview.consolidated_layout.holding_count || 0}
                        </strong>
                      </span>
                      <span>
                        共同基金
                        <strong>
                          {excelMappingPreview.consolidated_layout.fund_count || 0}
                        </strong>
                      </span>
                      <span>
                        台股 / ETF
                        <strong>
                          {excelMappingPreview.consolidated_layout.tw_count || 0}
                        </strong>
                      </span>
                      <span>
                        美股 / ETF
                        <strong>
                          {excelMappingPreview.consolidated_layout.us_count || 0}
                        </strong>
                      </span>
                      <span>
                        預估週配息
                        <strong>TWD</strong>
                      </span>
                    </div>
                    <div className="nexus-excel-source-grid nexus-excel-source-grid--report">
                      <label>
                        工作表
                        <select
                          value={excelConsolidatedDraft.sheet_name}
                          onChange={(event) =>
                            setExcelConsolidatedDraft({
                              ...excelConsolidatedDraft,
                              sheet_name: event.target.value,
                            })
                          }
                        >
                          {excelMappingPreview.sheets.map((sheet) => (
                            <option key={sheet.sheet_name} value={sheet.sheet_name}>
                              {sheet.sheet_name}
                            </option>
                          ))}
                        </select>
                      </label>
                      {CONSOLIDATED_ROW_FIELDS.map((field) => (
                        <label key={field.key}>
                          {field.label}
                          <input
                            type="number"
                            min="1"
                            value={excelConsolidatedDraft[field.key]}
                            onChange={(event) =>
                              setExcelConsolidatedDraft({
                                ...excelConsolidatedDraft,
                                [field.key]: event.target.value,
                              })
                            }
                          />
                        </label>
                      ))}
                      <label className="nexus-check-field">
                        <input
                          type="checkbox"
                          checked={excelConsolidatedDraft.include_zero_quantity}
                          onChange={(event) =>
                            setExcelConsolidatedDraft({
                              ...excelConsolidatedDraft,
                              include_zero_quantity: event.target.checked,
                            })
                          }
                        />
                        顯示零持有量
                      </label>
                    </div>
                    <div className="nexus-excel-mapping-grid nexus-excel-mapping-grid--report">
                      {CONSOLIDATED_COLUMN_FIELDS.map((field) => (
                        <label key={field.key}>
                          <span>{field.label}</span>
                          <select
                            value={excelConsolidatedDraft[field.key]}
                            onChange={(event) =>
                              setExcelConsolidatedDraft({
                                ...excelConsolidatedDraft,
                                [field.key]: event.target.value,
                              })
                            }
                          >
                            {EXCEL_COLUMN_OPTIONS.map((index) => (
                              <option key={index} value={String(index)}>
                                {excelColumnLetter(index)}
                              </option>
                            ))}
                          </select>
                        </label>
                      ))}
                    </div>
                    <div className="nexus-asset-defaults-grid">
                      {(
                        [
                          ['fund', '共同基金'],
                          ['tw', '台股 / ETF'],
                          ['us', '美股 / ETF'],
                        ] as const
                      ).map(([prefix, label]) => (
                        <div key={prefix}>
                          <strong>{label}</strong>
                          <label>
                            市場
                            <select
                              value={excelConsolidatedDraft[`${prefix}_market`]}
                              onChange={(event) =>
                                setExcelConsolidatedDraft({
                                  ...excelConsolidatedDraft,
                                  [`${prefix}_market`]: event.target.value,
                                })
                              }
                            >
                              <option value="FUND">共同基金</option>
                              <option value="TW">台灣</option>
                              <option value="US">美國</option>
                              <option value="OTHER">其他</option>
                            </select>
                          </label>
                          <label>
                            類型
                            <select
                              value={excelConsolidatedDraft[`${prefix}_asset_type`]}
                              onChange={(event) =>
                                setExcelConsolidatedDraft({
                                  ...excelConsolidatedDraft,
                                  [`${prefix}_asset_type`]: event.target.value,
                                })
                              }
                            >
                              <option value="AUTO">自動</option>
                              <option value="FUND">共同基金</option>
                              <option value="ETF">ETF</option>
                              <option value="STOCK">股票</option>
                            </select>
                          </label>
                          <label>
                            標的幣別
                            <select
                              value={excelConsolidatedDraft[`${prefix}_currency`]}
                              onChange={(event) =>
                                setExcelConsolidatedDraft({
                                  ...excelConsolidatedDraft,
                                  [`${prefix}_currency`]: event.target.value,
                                })
                              }
                            >
                              <option value="TWD">TWD</option>
                              <option value="USD">USD</option>
                              <option value="JPY">JPY</option>
                              <option value="EUR">EUR</option>
                            </select>
                          </label>
                          <label>
                            本金幣別
                            <select
                              value={
                                excelConsolidatedDraft[
                                  `${prefix}_principal_currency`
                                ]
                              }
                              onChange={(event) =>
                                setExcelConsolidatedDraft({
                                  ...excelConsolidatedDraft,
                                  [`${prefix}_principal_currency`]:
                                    event.target.value,
                                })
                              }
                            >
                              <option value="TWD">TWD</option>
                              <option value="USD">USD</option>
                              <option value="JPY">JPY</option>
                              <option value="EUR">EUR</option>
                              <option value="AUTO">同標的幣別</option>
                            </select>
                          </label>
                        </div>
                      ))}
                    </div>
                    <div className="nexus-excel-preview">
                      <div className="nexus-excel-preview-head">
                        <strong>報酬總表預覽</strong>
                        <span>顯示 TWD · 配息換算每週</span>
                      </div>
                      <div className="nexus-report-preview-table">
                        <div className="nexus-report-preview-row nexus-report-preview-row--head">
                          <span>代號 / 名稱</span>
                          <span>類別</span>
                          <span>數量</span>
                          <span>年化配息率</span>
                          <span>預估週配息</span>
                        </div>
                        {(excelMappingPreview.consolidated_layout.sample_holdings || []).map(
                          (holding, index) => (
                            <div className="nexus-report-preview-row" key={`${holding.symbol}:${index}`}>
                              <span>{holding.symbol || holding.name || '-'}</span>
                              <span>
                                {investmentCodeLabel(holding.asset_type || holding.market)}
                              </span>
                              <span>{formatInvestmentNumber(holding.quantity, 3)}</span>
                              <span>
                                {formatInvestmentNumber(
                                  holding.annual_dividend_yield_percent,
                                  2
                                )}%
                              </span>
                              <span>
                                NT${' '}
                                {formatInvestmentNumber(
                                  holding.estimated_weekly_dividend_twd,
                                  2
                                )}
                              </span>
                            </div>
                          )
                        )}
                      </div>
                    </div>
                  </>
                ) : excelLayoutMode === 'horizontal_matrix' ? (
                  <>
                    <div className="nexus-horizontal-sheet-list">
                      {excelHorizontalSheets.map((sheet) => (
                        <div
                          className={`nexus-horizontal-sheet${sheet.enabled ? ' is-selected' : ''}`}
                          key={sheet.sheet_name}
                        >
                          <label className="nexus-horizontal-sheet-toggle">
                            <input
                              type="checkbox"
                              checked={sheet.enabled}
                              onChange={(event) =>
                                updateHorizontalSheet(sheet.sheet_name, {
                                  enabled: event.target.checked,
                                })
                              }
                            />
                            <span>
                              <strong>{sheet.sheet_name}</strong>
                              {sheet.category_label} · {sheet.holding_count} 筆
                            </span>
                          </label>
                          <div className="nexus-horizontal-sheet-fields">
                            <label>
                              市場
                              <select
                                value={sheet.market}
                                onChange={(event) =>
                                  updateHorizontalSheet(sheet.sheet_name, {
                                    market: event.target.value,
                                  })
                                }
                              >
                                <option value="TW">台灣</option>
                                <option value="US">美國</option>
                                <option value="FUND">共同基金</option>
                                <option value="OTHER">其他</option>
                              </select>
                            </label>
                            <label>
                              類型
                              <select
                                value={sheet.asset_type}
                                onChange={(event) =>
                                  updateHorizontalSheet(sheet.sheet_name, {
                                    asset_type: event.target.value,
                                  })
                                }
                              >
                                <option value="AUTO">自動</option>
                                <option value="ETF">ETF</option>
                                <option value="STOCK">股票</option>
                                <option value="FUND">共同基金</option>
                              </select>
                            </label>
                            <label>
                              幣別
                              <select
                                value={sheet.currency}
                                onChange={(event) =>
                                  updateHorizontalSheet(sheet.sheet_name, {
                                    currency: event.target.value,
                                  })
                                }
                              >
                                <option value="TWD">TWD</option>
                                <option value="USD">USD</option>
                                <option value="JPY">JPY</option>
                                <option value="EUR">EUR</option>
                              </select>
                            </label>
                            {(
                              [
                                ['header_row_number', '標題列'],
                                ['holding_row_number', '持有列'],
                                ['price_row_number', '價格列'],
                                ['quantity_row_number', '數量列'],
                                ['group_width', '每組欄寬'],
                              ] as const
                            ).map(([key, label]) => (
                              <label key={key}>
                                {label}
                                <input
                                  type="number"
                                  min={key === 'group_width' ? 2 : 1}
                                  value={sheet[key]}
                                  onChange={(event) =>
                                    updateHorizontalSheet(sheet.sheet_name, {
                                      [key]: event.target.value,
                                    })
                                  }
                                />
                              </label>
                            ))}
                            <label>
                              起始欄
                              <select
                                value={sheet.first_asset_column_index}
                                onChange={(event) =>
                                  updateHorizontalSheet(sheet.sheet_name, {
                                    first_asset_column_index: event.target.value,
                                  })
                                }
                              >
                                {EXCEL_COLUMN_OPTIONS.map((index) => (
                                  <option key={index} value={String(index)}>
                                    {excelColumnLetter(index)}
                                  </option>
                                ))}
                              </select>
                            </label>
                          </div>
                        </div>
                      ))}
                    </div>
                    <div className="nexus-excel-preview">
                      <div className="nexus-excel-preview-head">
                        <strong>合併預覽</strong>
                        <span>
                          {selectedHorizontalSheets.length} 張 · {horizontalHoldingCount} 筆
                        </span>
                      </div>
                      <div className="nexus-report-preview-table">
                        {horizontalSampleHoldings.map((holding, index) => (
                          <div className="nexus-report-preview-row" key={`${holding.source_sheet}:${holding.symbol}:${index}`}>
                            <span>{holding.symbol || holding.name || '-'}</span>
                            <span>{holding.source_sheet}</span>
                            <span>{investmentCodeLabel(holding.asset_type)}</span>
                            <span>{formatInvestmentNumber(holding.quantity, 3)}</span>
                            <span>{holding.currency || 'TWD'}</span>
                          </div>
                        ))}
                      </div>
                    </div>
                  </>
                ) : (
                  <>
                <div className="nexus-excel-source-grid">
                  <label>
                    工作表
                    <select
                      value={selectedExcelSheet.sheet_name}
                      onChange={(event) => {
                        const sheet = excelMappingPreview.sheets.find(
                          (item) => item.sheet_name === event.target.value
                        )
                        if (sheet) selectExcelSheet(excelMappingPreview, sheet, true)
                      }}
                    >
                      {excelMappingPreview.sheets.map((sheet) => (
                        <option key={sheet.sheet_name} value={sheet.sheet_name}>
                          {sheet.sheet_name} · {sheet.row_count} 列
                        </option>
                      ))}
                    </select>
                  </label>
                  <label>
                    標題列
                    <input
                      type="number"
                      min="1"
                      max={Math.max(1, selectedExcelSheet.preview_row_count)}
                      value={excelHeaderRow}
                      onChange={(event) => {
                        const rowNumber = Math.max(
                          1,
                          Number(event.target.value) || 1
                        )
                        setExcelHeaderRow(rowNumber)
                        setExcelDataStartRow(rowNumber + 1)
                      }}
                    />
                  </label>
                  <label>
                    資料起始列
                    <input
                      type="number"
                      min="1"
                      max={Math.max(1, selectedExcelSheet.preview_row_count)}
                      value={excelDataStartRow}
                      onChange={(event) =>
                        setExcelDataStartRow(
                          Math.max(1, Number(event.target.value) || 1)
                        )
                      }
                    />
                  </label>
                  <button
                    type="button"
                    onClick={() =>
                      selectExcelSheet(
                        excelMappingPreview,
                        selectedExcelSheet,
                        false
                      )
                    }
                  >
                    套用自動建議
                  </button>
                  <button
                    type="button"
                    onClick={() => void openExcelMapper(true)}
                    disabled={Boolean(busyAction)}
                  >
                    選擇其他 Excel
                  </button>
                </div>
                <div className="nexus-excel-mapping-grid">
                  {EXCEL_MAPPING_FIELDS.map((field) => (
                    <label key={field.key}>
                      <span>
                        {field.label}
                        {field.required ? <em>必要</em> : null}
                      </span>
                      <select
                        required={field.required}
                        value={excelColumnMapping[field.key] ?? ''}
                        onChange={(event) =>
                          setExcelColumnMapping({
                            ...excelColumnMapping,
                            [field.key]: event.target.value,
                          })
                        }
                      >
                        <option value="">不讀取</option>
                        {excelHeaderValues.map((header, index) => (
                          <option key={`${field.key}:${index}`} value={String(index)}>
                            {excelColumnLetter(index)} · {header || '空白欄'}
                          </option>
                        ))}
                      </select>
                    </label>
                  ))}
                </div>
                <div className="nexus-excel-preview">
                  <div className="nexus-excel-preview-head">
                    <strong>映射預覽</strong>
                    <span>第 {excelDataStartRow} 列起</span>
                  </div>
                  <div className="nexus-excel-preview-table">
                    <div className="nexus-excel-preview-row nexus-excel-preview-row--head">
                      {EXCEL_MAPPING_FIELDS.map((field) => (
                        <span key={field.key}>{field.label}</span>
                      ))}
                    </div>
                    {mappedExcelPreviewRows.length > 0 ? (
                      mappedExcelPreviewRows.map((row, rowIndex) => (
                        <div
                          className="nexus-excel-preview-row"
                          key={`${selectedExcelSheet.sheet_name}:${excelDataStartRow}:${rowIndex}`}
                        >
                          {EXCEL_MAPPING_FIELDS.map((field) => {
                            const columnIndex = excelColumnMapping[field.key]
                            return (
                              <span key={field.key}>
                                {columnIndex === undefined || columnIndex === ''
                                  ? '-'
                                  : row[Number(columnIndex)] || '-'}
                              </span>
                            )
                          })}
                        </div>
                      ))
                    ) : (
                      <p>此設定暫無可預覽資料</p>
                    )}
                  </div>
                </div>
                  </>
                )}
                <p className="nexus-excel-mapper-message">{message}</p>
                <div className="nexus-excel-mapper-actions">
                  <button
                    type="button"
                    onClick={() => setExcelMapperOpen(false)}
                    disabled={Boolean(busyAction)}
                  >
                    取消
                  </button>
                  <button
                    type="submit"
                    className="nexus-primary"
                    disabled={Boolean(busyAction)}
                  >
                    套用並重新匯入
                  </button>
                </div>
              </form>
            ) : null}
            {holdingEditorOpen ? (
              <form
                className="nexus-holding-editor"
                onSubmit={(event) => void saveHolding(event)}
              >
                <div className="nexus-holding-editor-head">
                  <strong>{holdingDraft.holding_id ? '修改持股' : '新增持股'}</strong>
                  <span>本機修訂 · 不回寫來源檔</span>
                </div>
                <div className="nexus-holding-editor-grid">
                  <label>
                    代號
                    <input
                      required
                      value={holdingDraft.symbol}
                      onChange={(event) =>
                        setHoldingDraft({
                          ...holdingDraft,
                          symbol: event.target.value.toUpperCase(),
                        })
                      }
                    />
                  </label>
                  <label>
                    名稱
                    <input
                      value={holdingDraft.name}
                      onChange={(event) =>
                        setHoldingDraft({ ...holdingDraft, name: event.target.value })
                      }
                    />
                  </label>
                  <label>
                    市場
                    <select
                      value={holdingDraft.market}
                      onChange={(event) =>
                        setHoldingDraft({ ...holdingDraft, market: event.target.value })
                      }
                    >
                      <option value="TW">台灣</option>
                      <option value="US">美國</option>
                      <option value="HK">香港</option>
                      <option value="FUND">共同基金</option>
                      <option value="CRYPTO">加密資產</option>
                      <option value="OTHER">其他</option>
                    </select>
                  </label>
                  <label>
                    資產類型
                    <select
                      value={holdingDraft.asset_type}
                      onChange={(event) =>
                        setHoldingDraft({ ...holdingDraft, asset_type: event.target.value })
                      }
                    >
                      <option value="STOCK">股票</option>
                      <option value="ETF">ETF</option>
                      <option value="FUND">共同基金</option>
                      <option value="BOND">債券</option>
                      <option value="CRYPTO">加密資產</option>
                      <option value="OTHER">其他</option>
                    </select>
                  </label>
                  {holdingDraft.asset_type === 'FUND' ||
                  holdingDraft.market === 'FUND' ? (
                    <>
                      <label>
                        基金代碼
                        <input
                          value={holdingDraft.fund_code}
                          onChange={(event) =>
                            setHoldingDraft({
                              ...holdingDraft,
                              fund_code: event.target.value.toUpperCase(),
                            })
                          }
                        />
                      </label>
                      <label>
                        ISIN
                        <input
                          value={holdingDraft.fund_isin}
                          onChange={(event) =>
                            setHoldingDraft({
                              ...holdingDraft,
                              fund_isin: event.target.value.toUpperCase(),
                            })
                          }
                        />
                      </label>
                      <label>
                        級別
                        <input
                          value={holdingDraft.fund_share_class}
                          onChange={(event) =>
                            setHoldingDraft({
                              ...holdingDraft,
                              fund_share_class: event.target.value,
                            })
                          }
                        />
                      </label>
                      <label>
                        網路報價代號
                        <input
                          value={holdingDraft.fund_quote_symbol}
                          onChange={(event) =>
                            setHoldingDraft({
                              ...holdingDraft,
                              fund_quote_symbol: event.target.value.toUpperCase(),
                            })
                          }
                        />
                      </label>
                    </>
                  ) : null}
                  <label>
                    數量
                    <input
                      required
                      type="number"
                      min="0"
                      step="any"
                      value={holdingDraft.quantity}
                      onChange={(event) =>
                        setHoldingDraft({ ...holdingDraft, quantity: event.target.value })
                      }
                    />
                  </label>
                  <label>
                    平均成本
                    <input
                      required
                      type="number"
                      min="0"
                      step="any"
                      value={holdingDraft.average_cost}
                      onChange={(event) =>
                        setHoldingDraft({ ...holdingDraft, average_cost: event.target.value })
                      }
                    />
                  </label>
                  <label>
                    幣別
                    <input
                      required
                      value={holdingDraft.currency}
                      onChange={(event) =>
                        setHoldingDraft({
                          ...holdingDraft,
                          currency: event.target.value.toUpperCase(),
                        })
                      }
                    />
                  </label>
                  <label>
                    本金幣別
                    <input
                      required
                      value={holdingDraft.principal_currency}
                      onChange={(event) =>
                        setHoldingDraft({
                          ...holdingDraft,
                          principal_currency: event.target.value.toUpperCase(),
                        })
                      }
                    />
                  </label>
                  {(
                    [
                      ['principal_amount', '原始本金'],
                      ['principal_twd', '本金 TWD'],
                      ['current_value_twd', '目前市值 TWD'],
                      ['dividend_amount_twd', '配息金額 TWD'],
                      ['dividend_per_unit', '單位配息'],
                      ['monthly_dividend_twd', '每月配息 TWD'],
                      ['annual_dividend_yield_percent', '年化配息率 %'],
                      ['payback_rate_percent', '回本率 %'],
                    ] as const
                  ).map(([key, label]) => (
                    <label key={key}>
                      {label}
                      <input
                        type="number"
                        min="0"
                        step="any"
                        value={holdingDraft[key]}
                        onChange={(event) =>
                          setHoldingDraft({
                            ...holdingDraft,
                            [key]: event.target.value,
                          })
                        }
                      />
                    </label>
                  ))}
                </div>
                <div className="nexus-holding-editor-actions">
                  <button
                    type="button"
                    onClick={() => setHoldingEditorOpen(false)}
                    disabled={Boolean(busyAction)}
                  >
                    取消
                  </button>
                  <button
                    type="submit"
                    className="nexus-primary"
                    disabled={Boolean(busyAction)}
                  >
                    儲存持股
                  </button>
                </div>
              </form>
            ) : null}
            <div className="nexus-holding-table">
              <div className="nexus-holding-head">
                <span>代號</span>
                <span>名稱</span>
                <span>市場</span>
                <span>數量</span>
                <span>平均成本</span>
                <span>本金</span>
                <span>幣別</span>
                <span>市值 TWD</span>
                <span>配息頻率</span>
                <span>週配息 TWD</span>
                <span>操作</span>
              </div>
              {visibleHoldings.length === 0 ? (
                <p className="nexus-empty-line">尚無持股資料</p>
              ) : (
                visibleHoldings.map((holding, index) => (
                  <div
                    key={`${holding.symbol || 'holding'}:${index}`}
                    className="nexus-holding-row"
                  >
                    <strong>{holding.symbol || '-'}</strong>
                    <span>
                      {holding.name || '-'}
                      {holding.fund_identity_status ? (
                        <small>
                          {holding.fund_identity_status === 'confirmed'
                            ? `已辨識 ${holding.fund_quote_symbol || ''}`
                            : holding.fund_identity_status === 'suggested'
                              ? `待確認 ${holding.fund_candidate_symbol || ''}`
                              : '基金代號待補'}
                        </small>
                      ) : null}
                    </span>
                    <span>{investmentCodeLabel(holding.market)}</span>
                    <span>{formatInvestmentNumber(holding.quantity, 4)}</span>
                    <span>
                      {formatInvestmentNumber(holding.average_cost, 4)}
                    </span>
                    <span>
                      {holding.principal_currency || holding.currency || '-'}{' '}
                      {formatInvestmentNumber(holding.principal_amount, 2)} · NT${' '}
                      {formatInvestmentNumber(holding.principal_twd, 2)}
                    </span>
                    <span>{holding.currency || '-'}</span>
                    <span>
                      NT${' '}
                      {formatInvestmentNumber(
                        holding.web_current_value_twd ?? holding.current_value_twd,
                        2
                      )}
                    </span>
                    <span>{holding.dividend_frequency_label || '待同步'}</span>
                    <span>
                      NT${' '}
                      {formatInvestmentNumber(
                        holding.estimated_weekly_dividend_twd,
                        2
                      )}
                    </span>
                    <div className="nexus-holding-row-actions">
                      <button
                        type="button"
                        onClick={() => openHoldingEditor(holding)}
                        disabled={Boolean(busyAction)}
                      >
                        編輯
                      </button>
                      <button
                        type="button"
                        className="nexus-danger"
                        onClick={() => void deleteHolding(holding)}
                        disabled={Boolean(busyAction)}
                      >
                        刪除
                      </button>
                    </div>
                  </div>
                ))
              )}
            </div>
          </section>
        </section>

        <aside className="nexus-column nexus-column--right">
          <section className="nexus-surface nexus-local-ai-status">
            <div className="nexus-section-head">
              <span>星澄</span>
              <strong>{localAiStatus?.watch_status_label || '等待監測'}</strong>
            </div>
            <div
              className={`nexus-ai-score nexus-ai-score--${localAiStatus?.state || 'empty'}`}
            >
              <strong>{localAiScore}</strong>
              <span>{localAiStatusLabel}</span>
            </div>
            <p>{localAiRecommendation}</p>
            {localAiDecisionBrief ? (
              <div className="nexus-local-ai-decision">
                <span>星澄決策摘要</span>
                <strong>
                  信心{' '}
                  {localAiConfidence?.label ||
                    localAiStatus?.confidence_label ||
                    '-'}
                  {' · '}
                  行動 {localAiStatus?.action_count ?? localAiActionPlan.length}
                  {' · '}
                  觸發{' '}
                  {localAiStatus?.trigger_count ?? localAiWatchTriggers.length}
                </strong>
                <p>{localAiDecisionBrief}</p>
              </div>
            ) : null}
            <div className="nexus-local-ai-meta">
              <span>{localAiStatus?.coverage_label || '等待持股資料'}</span>
              <span
                className={`nexus-network-pill nexus-network-pill--${localAiQuoteHealth}`}
              >
                {localAiQuoteHealthLabel}
              </span>
              <span>{localAiNetworkLabel}</span>
              <span>{localAiVerificationLabel}</span>
              <span>來源 {localAiProviderCount}</span>
              <span>
                信心 {localAiSourceConfidence?.grade || '-'} ·{' '}
                {formatInvestmentNumber(localAiSourceConfidence?.score, 0)}
              </span>
              <span>
                刷新 {localAiStatus?.refreshed_holding_count || 0} · 快取{' '}
                {localAiStatus?.reused_holding_count || 0}
              </span>
              <span>{localAiExplanation?.mode_label || localAiStatus?.explanation_mode_label || '規則引擎說明'}</span>
              <span>風險 {localAiStatus?.warning_count ?? 0}</span>
              <span>重大 {localAiStatus?.critical_count ?? 0}</span>
            </div>
            {localAiExplanation?.text ? (
              <div className="nexus-local-ai-explanation">
                <strong>{localAiExplanation.mode_label}</strong>
                <p>{localAiExplanation.text}</p>
              </div>
            ) : null}
            {localAiQuoteGapCount > 0 ? (
              <div className="nexus-quote-gaps">
                <span>報價缺口 {localAiQuoteGapCount}</span>
                {localAiQuoteGaps.length === 0 ? (
                  <p>有報價缺口，但目前沒有取得明細；請重新執行星澄。</p>
                ) : (
                  localAiQuoteGaps.map((gap, index) => (
                    <article key={`${gap.symbol || 'quote-gap'}:${index}`}>
                      <strong>
                        {gap.symbol || '-'}
                        {gap.name ? ` · ${gap.name}` : ''}
                      </strong>
                      <span>
                        {gap.market || '-'} ·{' '}
                        {gap.reason || gap.status || '未取得可信報價'}
                      </span>
                      <p>
                        {gap.detail ||
                          gap.action ||
                          '請檢查代號、市場或重新執行星澄。'}
                      </p>
                    </article>
                  ))
                )}
              </div>
            ) : null}
            {localAiActions.length > 0 ? (
              <ul className="nexus-local-ai-actions">
                {localAiActions.map((action, index) => (
                  <li key={`${action}:${index}`}>{action}</li>
                ))}
              </ul>
            ) : null}
            <div className="nexus-risk-board">
              <span>持倉風險預告</span>
              {localAiWarnings.length === 0 ? (
                <p>尚無風險預告</p>
              ) : (
                localAiWarnings.map((warning, index) => (
                  <article
                    key={`${warning.code || 'risk'}:${warning.symbol || 'portfolio'}:${index}`}
                    className={`nexus-risk-row nexus-risk-row--${warning.severity || 'info'}`}
                  >
                    <strong>
                      {warning.title || warning.code || '風險提示'}
                    </strong>
                    <span>{warning.detail || warning.action || '-'}</span>
                  </article>
                ))
              )}
            </div>
            <div className="nexus-command-suggestions">
              <span>星澄命令建議</span>
              <div>
                {localAiCommands.length === 0 ? (
                  <code>匯入持股後可下達本地分析命令</code>
                ) : (
                  localAiCommands.map((command, index) => (
                    <button
                      key={`${command}:${index}`}
                      type="button"
                      className="nexus-command-chip"
                      onClick={() => applyLocalCommandPreset(command)}
                      disabled={Boolean(busyAction)}
                    >
                      {command}
                    </button>
                  ))
                )}
              </div>
            </div>
          </section>

          <section className="nexus-surface">
            <div className="nexus-section-head">
              <span>星澄命令</span>
              <strong>{investmentWatch.investmentRuns.length}</strong>
            </div>
            <form
              className="nexus-local-ai-command"
              onSubmit={(event) => {
                event.preventDefault()
                void investmentWatch.sendLocalRiskCommand()
              }}
            >
              <label htmlFor="local-risk-command">輸入命令給星澄</label>
              <div
                className="nexus-command-presets"
                aria-label="星澄常用命令"
              >
                {LOCAL_AI_COMMAND_PRESETS.map((preset) => (
                  <button
                    key={preset.label}
                    type="button"
                    onClick={() => applyLocalCommandPreset(preset.command)}
                    disabled={
                      Boolean(busyAction) ||
                      investmentWatch.investmentHoldings.length === 0
                    }
                  >
                    {preset.label}
                  </button>
                ))}
              </div>
              <input
                id="local-risk-command"
                value={investmentWatch.localRiskCommand}
                onChange={(event) =>
                  investmentWatch.setLocalRiskCommand(event.target.value)
                }
                placeholder={
                  investmentWatch.investmentHoldings.length === 0
                    ? '請先讀取 Excel 檔'
                    : '預設自動連網報價，例如：只看 AAPL 跌破成本 3% 集中度 30%'
                }
                disabled={
                  Boolean(busyAction) ||
                  investmentWatch.investmentHoldings.length === 0
                }
              />
              <button
                type="submit"
                className="nexus-primary nexus-command-submit"
                disabled={
                  Boolean(busyAction) ||
                  investmentWatch.investmentHoldings.length === 0
                }
              >
                {busyAction === 'investment:local-risk-command'
                  ? '星澄執行中...'
                  : '送出星澄命令'}
              </button>
            </form>
            {localAiCommandPreview ? (
              <div className="nexus-command-result">
                <span>星澄回覆</span>
                <pre>{localAiCommandPreview}</pre>
              </div>
            ) : null}
            {localAiActionPlan.length > 0 ? (
              <div className="nexus-action-plan">
                <span>星澄行動計畫</span>
                {localAiActionPlan.map((item, index) => (
                  <article
                    key={`${item.symbol || 'portfolio'}:${item.title || 'action'}:${index}`}
                    className={`nexus-plan-row nexus-plan-row--${item.priority || 'monitor'}`}
                  >
                    <strong>{item.title || item.symbol || '行動'}</strong>
                    <span>
                      {item.due || '-'} · {item.risk_level_label || '-'}
                    </span>
                    <p>{item.action || '-'}</p>
                  </article>
                ))}
              </div>
            ) : null}
            {localAiWatchTriggers.length > 0 ? (
              <div className="nexus-watch-triggers">
                <span>監測觸發條件</span>
                {localAiWatchTriggers.map((item, index) => (
                  <article
                    key={`${item.symbol || 'trigger'}:${item.trigger || 'condition'}:${index}`}
                    className={`nexus-trigger-row nexus-trigger-row--${item.severity || 'info'}`}
                  >
                    <strong>
                      {item.symbol || '-'} · {item.trigger || '-'}
                    </strong>
                    <span>{item.threshold_text || item.threshold || '-'}</span>
                    <p>{item.action || '-'}</p>
                  </article>
                ))}
              </div>
            ) : null}
            <div className="nexus-run-list">
              {latestInvestmentRuns.length === 0 ? (
                <p className="nexus-empty-line">尚無分析紀錄</p>
              ) : (
                latestInvestmentRuns.map((run) => {
                  const preview = (run.content || run.error || '').trim()
                  return (
                    <article key={run.run_id} className="nexus-run-row">
                      <strong>{investmentRunLabel(run)}</strong>
                      <span>
                        {investmentStatusLabel(run.status)} ·{' '}
                        {formatInvestmentClock(run.created_at)}
                      </span>
                      {preview ? (
                        <pre className="nexus-run-preview">{preview}</pre>
                      ) : null}
                    </article>
                  )
                })
              )}
            </div>
          </section>

          <section className="nexus-surface nexus-system">
            <div className="nexus-section-head">
              <span>手機遠端同步</span>
              <strong>{mobileSyncStatusLabel}</strong>
            </div>
            <div className="nexus-mobile-sync-grid">
              <div>
                <span>配對碼</span>
                <strong>{mobileSyncPairingCode}</strong>
              </div>
              <div>
                <span>連線模式</span>
                <strong>{mobileSync?.mode_label || '-'}</strong>
              </div>
              <div>
                <span>手機程式</span>
                <strong>
                  {mobileSync?.platform?.clients?.includes('android_native')
                    ? `Android 原生 · ${mobileSync.platform.foreground_sync_seconds || 2} 秒同步`
                    : '尚未支援'}
                </strong>
              </div>
              <div>
                <span>不同網路網址</span>
                <strong title={mobileSyncRemoteUrl}>
                  {mobileSyncRemoteUrl || '未設定橋接 URL'}
                </strong>
              </div>
              <div>
                <span>本機/區網網址</span>
                <strong title={mobileSyncLocalUrl}>
                  {mobileSyncLocalUrl || '-'}
                </strong>
              </div>
              <div className="nexus-mobile-sync-link">
                <span>手機網址</span>
                <strong title={mobileSyncPhoneUrl}>
                  {mobileSyncPhoneUrl || '-'}
                </strong>
              </div>
            </div>
            <form
              className="nexus-mobile-sync-form"
              onSubmit={(event) => {
                event.preventDefault()
                void investmentWatch.saveMobileSyncRemoteUrl(
                  mobileSyncRemoteDraft
                )
              }}
            >
              <label htmlFor="mobile-sync-remote-url">不同網路橋接 URL</label>
              <input
                id="mobile-sync-remote-url"
                value={mobileSyncRemoteDraft}
                onChange={(event) =>
                  setMobileSyncRemoteDraft(event.target.value)
                }
                placeholder="https://your-remote-bridge.example"
                disabled={Boolean(busyAction)}
              />
              <div className="nexus-mobile-sync-actions">
                <button
                  type="submit"
                  className="nexus-primary"
                  disabled={Boolean(busyAction)}
                >
                  {busyAction === 'investment:mobile-sync'
                    ? '儲存中...'
                    : '儲存橋接'}
                </button>
                <button
                  type="button"
                  onClick={() => void investmentWatch.rotateMobileSyncPairing()}
                  disabled={Boolean(busyAction)}
                >
                  {busyAction === 'investment:mobile-sync-pairing'
                    ? '更新中...'
                    : '更新配對碼'}
                </button>
              </div>
            </form>
          </section>

          <section className="nexus-surface nexus-system">
            <div className="nexus-section-head">
              <span>系統</span>
              <strong>{socketLabel}</strong>
            </div>
            <span>狀態檔：{paths.state || '尚未載入'}</span>
            <span>工作區：{paths.workspace || '尚未載入'}</span>
            <p>星澄負責搜尋與投資分析；外部 AI 協作工具只討論星澄的分析結果。</p>
          </section>
        </aside>
      </section>
    </main>
  )
}
