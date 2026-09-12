﻿import { useCallback, useMemo, useState } from 'react'

import { useRef } from 'react'

type BackendRequest = (
  command: string,
  payload?: Record<string, unknown>,
  timeoutMs?: number
) => Promise<Record<string, unknown>>

type SetMessage = (message: string) => void
type SetBusyAction = (action: string) => void

export type InvestmentHolding = {
  holding_id?: string
  symbol?: string
  name?: string
  market?: string
  asset_type?: string
  quantity?: number
  average_cost?: number | null
  currency?: string
  principal_amount?: number | null
  principal_currency?: string
  principal_twd?: number | null
  web_current_price?: number | null
  web_current_price_currency?: string
  web_current_value_twd?: number | null
  market_data_source?: string
  market_data_updated_at?: string
  manually_edited?: boolean
  dividend_amount_twd?: number | null
  dividend_per_unit?: number | null
  monthly_dividend_twd?: number | null
  annual_dividend_yield_percent?: number | null
  payback_rate_percent?: number | null
  current_value_twd?: number | null
  estimated_annual_dividend_twd?: number | null
  estimated_weekly_dividend_twd?: number | null
  dividend_source?: string
  dividend_updated_at?: string
  dividend_frequency?: string
  dividend_frequency_label?: string
  dividend_frequency_per_year?: number | null
  dividend_frequency_source?: string
  fund_code?: string
  fund_isin?: string
  fund_share_class?: string
  fund_quote_symbol?: string
  fund_candidate_symbol?: string
  fund_identity_status?: string
  fund_identity_confidence?: number | null
  fund_identity_candidates?: Array<Record<string, unknown>>
}

export type InvestmentRun = {
  run_id: string
  role: string
  provider: string
  status: string
  content: string
  error: string
  created_at: string
}

export type LocalAiProductStatus = {
  state?: string
  state_label?: string
  score?: number
  risk_level?: string
  risk_level_label?: string
  decision_summary?: string
  confidence_label?: string
  confidence_score?: number
  action_count?: number
  trigger_count?: number
  network_enabled?: boolean
  network_mode?: string
  network_mode_label?: string
  quote_health?: string
  quote_health_label?: string
  network_policy?: string
  network_context?: LocalAiNetworkContext | null
  recommendation?: string
  next_actions?: string[]
  command_suggestions?: string[]
  watch_status?: string
  watch_status_label?: string
  offline_mode?: boolean
  has_portfolio?: boolean
  portfolio_count?: number
  warning_count?: number
  notice_count?: number
  critical_count?: number
  reused_holding_count?: number
  refreshed_holding_count?: number
  source_confidence?: {
    score?: number
    grade?: string
    label?: string
    provider_count?: number
    low_confidence_count?: number
  }
  explanation_mode?: string
  explanation_mode_label?: string
  explanation_text?: string
  calibration_applied?: boolean
  coverage_label?: string
  generated_at?: string
  analysis_owner?: string
  external_discussion_connected?: boolean
}

export type ExternalAiDiscussion = {
  ok?: boolean
  queued?: boolean
  provider?: string
  status?: string
  content?: string
  message?: string
  response_recipient?: string
  transport?: string
}

export type LocalAiRiskWarning = {
  severity?: string
  code?: string
  title?: string
  detail?: string
  action?: string
  symbol?: string
  metric?: number
  occurrence_count?: number
  symbols?: string[]
  rule_profile?: string
}

export type LocalAiCommandResult = {
  intent?: string
  sections?: string[]
  symbols?: string[]
  matched_symbols?: string[]
  missing_symbols?: string[]
  network_context?: LocalAiNetworkContext | null
  portfolio_score?: number
  portfolio_rating?: {
    rating?: string
    label?: string
    range?: string
  }
  risk_level?: string
  next_actions?: string[]
  confidence_summary?: LocalAiConfidenceSummary | null
  action_plan?: LocalAiActionPlanItem[]
  watch_triggers?: LocalAiWatchTrigger[]
  decision_brief?: string
  text?: string
}

export type LocalAiNetworkContext = {
  enabled?: boolean
  mode?: string
  mode_label?: string
  health?: string
  health_label?: string
  policy?: string
  quote_provider_count?: number
  quote_providers?: string[]
  successful_providers?: string[]
  failed_providers?: string[]
  attempt_count?: number
  holding_count?: number
  quoted_count?: number
  verified_quote_count?: number
  cross_checked_count?: number
  single_source_count?: number
  untrusted_quote_count?: number
  failed_quote_count?: number
  validation_issue_count?: number
  divergence_count?: number
  stale_quote_count?: number
  symbol_mismatch_count?: number
  currency_mismatch_count?: number
  validation_issues?: Array<{
    symbol?: string
    severity?: string
    code?: string
    title?: string
    detail?: string
  }>
  quote_gap_count?: number
  quote_gaps?: Array<{
    symbol?: string
    name?: string
    market?: string
    status?: string
    reason?: string
    detail?: string
    failed_providers?: string[]
    attempt_count?: number
    action?: string
  }>
  coverage_percent?: number | null
  coverage_label?: string
}

export type LocalAiConfidenceSummary = {
  score?: number
  label?: string
  sample_count?: number
  low_confidence_symbols?: string[]
}

export type LocalAiActionPlanItem = {
  priority?: string
  due?: string
  symbol?: string
  title?: string
  action?: string
  score?: number | null
  risk_level?: string
  risk_level_label?: string
  confidence_label?: string
  position_weight_percent?: number | null
  rationale?: string[]
}

export type LocalAiWatchTrigger = {
  symbol?: string
  trigger?: string
  severity?: string
  threshold?: number | null
  threshold_text?: string
  current_price?: number | null
  currency?: string
  action?: string
}

export type WorkbookScanQuality = {
  state?: string
  state_label?: string
  score?: number
  selected_sheet_name?: string
  header_row_number?: number
  header_mode?: string
  header_depth?: number
  valid_data_row_count?: number
  recommendation?: string
}

type WorkbookSheetScan = {
  sheet_name?: string
  header_row_number?: number | null
  data_start_row_number?: number | null
  header_mode?: string
  score?: number
  valid_data_row_count?: number
  usable?: boolean
}

export type { WorkbookSheetScan }

export type WorkbookScanSummary = {
  sheet_count?: number
  selected_sheet?: WorkbookSheetScan | null
  sheets?: WorkbookSheetScan[]
}

export type ExcelMappingSheetPreview = {
  sheet_index?: number
  sheet_name: string
  row_count: number
  preview_row_count: number
  column_count: number
  suggested_header_row_number?: number | null
  suggested_data_start_row_number?: number | null
  suggested_mapping?: Record<string, number>
  rows: string[][]
}

export type ExcelHorizontalHoldingPreview = {
  symbol?: string
  name?: string
  market?: string
  asset_type?: string
  quantity?: number
  average_cost?: number | null
  currency?: string
  annual_dividend_yield_percent?: number | null
  estimated_weekly_dividend_twd?: number | null
}

export type ExcelConsolidatedLayoutPreview = Record<string, unknown> & {
  detected?: boolean
  recommended?: boolean
  layout?: string
  sheet_name?: string
  holding_count?: number
  fund_count?: number
  tw_count?: number
  us_count?: number
  etf_count?: number
  stock_count?: number
  sample_holdings?: ExcelHorizontalHoldingPreview[]
}

export type ExcelHorizontalSheetPreview = {
  sheet_index?: number
  sheet_name: string
  detected?: boolean
  selected_by_default?: boolean
  category_label?: string
  market?: string
  asset_type?: string
  currency?: string
  header_row_number?: number
  holding_row_number?: number
  price_row_number?: number | null
  quantity_row_number?: number
  first_asset_column_index?: number
  first_asset_column_letter?: string
  group_width?: number
  holding_count?: number
  skipped_group_count?: number
  sample_holdings?: ExcelHorizontalHoldingPreview[]
}

export type ExcelHorizontalLayoutPreview = {
  detected?: boolean
  recommended?: boolean
  layout?: string
  sheet_count?: number
  selected_sheet_count?: number
  holding_count?: number
  supported_asset_classes?: string[]
  sheets: ExcelHorizontalSheetPreview[]
}

export type ExcelMappingPreview = {
  source_path: string
  file_name: string
  sheet_count: number
  selected_sheet_name?: string
  mapping_fields?: string[]
  required_fields?: string[]
  sheets: ExcelMappingSheetPreview[]
  horizontal_layout?: ExcelHorizontalLayoutPreview | null
  consolidated_layout?: ExcelConsolidatedLayoutPreview | null
  smart_repair?: {
    recommended?: boolean
    layout?: string
    sheet_name?: string
    header_row_number?: number
    data_start_row_number?: number
    column_mapping?: Record<string, number>
    confidence_score?: number
    confidence_label?: string
    reason?: string
    changes?: string[]
  } | null
}

export type ExcelImportProfile = {
  source_path?: string
  layout?: string
  sheet_name?: string
  header_row_number?: number
  data_start_row_number?: number
  column_mapping?: Record<string, number>
  sheets?: Array<Record<string, unknown>>
  updated_at?: string
}

export type InvestmentDiagnostics = {
  state?: string
  state_label?: string
  message?: string
  generated_at?: string
  data_quality?: {
    state?: string
    holding_count?: number
    invalid_symbol_count?: number
    decimal_symbol_count?: number
    invalid_symbol_percent?: number
    risk_analysis_suspended?: boolean
  }
  error_logging?: {
    enabled?: boolean
    count?: number
    path?: string
    format?: string
  }
  portfolio?: {
    source_path?: string
    file_name?: string
    holding_count?: number
    imported_at?: string
    age_hours?: number | null
    stale?: boolean
  }
  workbook?: {
    state?: string
    state_label?: string
    score?: number | null
    sheet_count?: number
    selected_sheet_name?: string
    header_row_number?: number | null
    header_depth?: number | null
    valid_data_row_count?: number | null
    recommendation?: string
  }
  xingcheng?: {
    state?: string
    state_label?: string
    score?: number | null
    risk_level?: string
    risk_level_label?: string
    watch_status_label?: string
    network_enabled?: boolean
    network_mode?: string
    network_mode_label?: string
    quote_health?: string
    quote_health_label?: string
    network_policy?: string
    quote_provider_count?: number
    quote_providers?: string[]
    verified_quote_count?: number
    cross_checked_count?: number
    single_source_count?: number
    untrusted_quote_count?: number
    validation_issue_count?: number
    divergence_count?: number
    stale_quote_count?: number
    symbol_mismatch_count?: number
    currency_mismatch_count?: number
    quote_gap_count?: number
    quote_gaps?: LocalAiNetworkContext['quote_gaps']
    coverage_percent?: number | null
    coverage_label?: string
    warning_count?: number
    critical_count?: number
    offline_mode?: boolean
    generated_at?: string
  }
  runs?: {
    count?: number
    latest?: {
      run_id?: string
      role?: string
      provider?: string
      status?: string
      created_at?: string
    }
  }
  boundaries?: {
    local_only?: boolean
    external_ai?: boolean
    service_commands?: string[]
  }
}

export type MobileSyncStatus = {
  enabled?: boolean
  running?: boolean
  mode?: string
  mode_label?: string
  remote_ready?: boolean
  remote_status_label?: string
  relay_required?: boolean
  port?: number
  bind_host?: string
  pairing_code?: string
  loopback_url?: string
  local_urls?: string[]
  remote_base_url?: string
  remote_url?: string
  access_scope?: string
  platform?: {
    name?: string
    contract_version?: number
    clients?: string[]
    source_of_truth?: string
    shared_repository?: boolean
    mobile_write_scope?: string
    foreground_sync_seconds?: number
    capabilities?: string[]
  }
  session_count?: number
  session_idle_minutes?: number
  pairing_active?: boolean
  pairing_expired?: boolean
  pairing_revoked?: boolean
  pairing_created_at?: string
  pairing_expires_at?: string
  pairing_revoked_at?: string
  pairing_ttl_hours?: number
  rate_limit?: string
  start_error?: string
}

export type InvestmentAnalyticsSnapshot = {
  version?: string
  generated_at?: string
  database_path?: string
  data_health?: {
    schema_version?: number
    counts?: Record<string, number>
    history_ready?: boolean
    ledger_ready?: boolean
    ledger_quality?: 'empty' | 'estimated_opening' | 'confirmed'
    warnings?: string[]
  }
  privacy?: {
    state_encryption?: string
    sensitive_field_encryption?: string
    platform_protected?: boolean
  }
  performance?: Record<string, unknown> & {
    status?: string
    current_value?: number
    current_cost?: number
    unrealized_pnl?: number
    unrealized_pnl_percent?: number | null
    realized_pnl?: number
    dividend_income?: number
    fees_and_taxes?: number
    reconciliation?: {
      status?: string
      symbol_count?: number
      matched_count?: number
      difference_count?: number
      coverage_percent?: number
      differences?: Array<Record<string, unknown>>
    }
    twr_percent?: number | null
    xirr_percent?: number | null
    annualized_volatility_percent?: number | null
    max_drawdown_percent?: number | null
    snapshot_count?: number
    equity_curve?: Array<{ date?: string; value?: number }>
    positions?: Array<Record<string, unknown>>
    attribution_by_currency?: Record<string, Record<string, number>>
  }
  risk?: Record<string, unknown> & {
    status?: string
    sample_count?: number
    annualized_volatility_percent?: number | null
    beta?: number | null
    var_95_one_day_percent?: number | null
    cvar_95_one_day_percent?: number | null
    max_drawdown_percent?: number | null
    benchmark?: string
    correlations?: Array<Record<string, unknown>>
    risk_contributions?: Array<Record<string, unknown>>
    exposures?: Record<string, Record<string, number>>
  }
  stress?: {
    status?: string
    current_value?: number
    scenarios?: Array<Record<string, unknown>>
  }
  ledger?: Record<string, unknown> & {
    transactions?: Array<Record<string, unknown>>
    transaction_count?: number
    confirmed_transaction_count?: number
    estimated_transaction_count?: number
    ledger_quality?: 'empty' | 'estimated_opening' | 'confirmed'
    opening_ledger?: {
      occurred_at?: string
      generated_count?: number
      active_holding_count?: number
      uncovered_count?: number
      uncovered_symbols?: string[]
      coverage_percent?: number
    }
    realized_pnl?: number
    dividend_income?: number
    fees_and_taxes?: number
  }
  events?: Array<Record<string, unknown>>
  alerts?: {
    rules?: Array<Record<string, unknown>>
    events?: Array<Record<string, unknown>>
    new_count?: number
    unacknowledged_count?: number
  }
  decisions?: Array<Record<string, unknown>>
  calibration?: Record<string, unknown> & {
    status?: string
    evaluated_count?: number
    pending_count?: number
    brier_score?: number | null
    calibration_label?: string
    accuracy_percent?: number | null
    average_confidence_percent?: number | null
    reliability_gap_percent?: number | null
    confidence_multiplier?: number
    confidence_buckets?: Array<Record<string, unknown>>
  }
}

export type InvestmentV3Snapshot = Record<string, unknown> & {
  version?: string
  investment_policy?: {
    investment_goal?: string
    time_horizon_years?: number
    risk_capacity?: string
    cash_need_percent?: number
    forbidden_assets?: string[]
    target_return_percent?: number | null
    human_approval_required?: boolean
    automatic_order_submission?: boolean
  }
  multi_currency?: Record<string, unknown> & {
    status?: string
    base_currency?: string
    market_value_base?: number
    total_pnl_base?: number
    asset_pnl_base?: number
    currency_pnl_base?: number
    missing_currencies?: string[]
    positions?: Array<Record<string, unknown>>
  }
  regime?: Record<string, unknown>
  factors?: Record<string, unknown> & {
    factors?: Array<Record<string, unknown>>
    symbol_contributions?: Array<Record<string, unknown>>
  }
  corporate_actions?: Record<string, unknown> & {
    actions?: Array<Record<string, unknown>>
    issues?: Array<Record<string, unknown>>
  }
  broker_imports?: Array<Record<string, unknown>>
  automation?: Record<string, unknown>
  notifications?: Record<string, unknown> & {
    channels?: Array<Record<string, unknown>>
    outbox?: Array<Record<string, unknown>>
  }
  model_governance?: Record<string, unknown>
  database_security?: Record<string, unknown>
  backups?: Array<Record<string, unknown>>
  audit_log?: Array<Record<string, unknown>>
}

export type InvestmentState = {
  updated_at?: string
  portfolio?: {
    source_path?: string
    file_name?: string
    holding_count?: number
    imported_at?: string
    source_created_at?: string
    manually_modified_at?: string
    manual_revision?: number
  } | null
  holdings?: InvestmentHolding[]
  ai_runs?: InvestmentRun[]
  workbook_scan?: WorkbookScanSummary | null
  workbook_scan_quality?: WorkbookScanQuality | null
  excel_import_profile?: ExcelImportProfile | null
  xingcheng_product_status?: LocalAiProductStatus | null
  xingcheng_risk_warnings?: LocalAiRiskWarning[]
  xingcheng_command_result?: LocalAiCommandResult | null
  xingcheng_action_plan?: LocalAiActionPlanItem[]
  xingcheng_watch_triggers?: LocalAiWatchTrigger[]
  xingcheng_confidence?: LocalAiConfidenceSummary | null
  xingcheng_decision_brief?: string
  xingcheng_network_context?: LocalAiNetworkContext | null
  xingcheng_explanation?: {
    mode?: string
    mode_label?: string
    model?: string
    text?: string
    facts_locked?: boolean
  } | null
  xingcheng_external_discussion?: ExternalAiDiscussion | null
  portfolio_versions?: Array<{
    version_id?: string
    created_at?: string
    reason?: string
    holding_count?: number
    file_name?: string
    manual_revision?: number
  }>
  fund_identity_sync?: Record<string, unknown> | null
  portfolio_memory?: string
  shared_memory?: string
  analytics?: InvestmentAnalyticsSnapshot | null
  v3?: InvestmentV3Snapshot | null
  dividend_sync?: Record<string, unknown> | null
  market_sessions?: {
    as_of?: string
    open_markets?: string[]
    markets?: Record<
      string,
      {
        is_open?: boolean
        timezone?: string
        schedule?: string
        local_time?: string
      }
    >
  } | null
}

export type InvestmentResult = {
  ok?: boolean
  message?: string
  not_modified?: boolean
  state_revision?: string
  state?: InvestmentState
  diagnostics?: InvestmentDiagnostics
  market_sessions?: InvestmentState['market_sessions']
  mobile_sync?: MobileSyncStatus
  report_path?: string
  [key: string]: unknown
}

type InvestmentWatchFeatureOptions = {
  request: BackendRequest
  setMessage: SetMessage
  setBusyAction: SetBusyAction
}

export type InvestmentWatchFeature = {
  investmentState: InvestmentState
  investmentDiagnostics: InvestmentDiagnostics | null
  investmentHoldings: InvestmentHolding[]
  investmentRuns: InvestmentRun[]
  localAiProductStatus: LocalAiProductStatus | null
  localAiRiskWarnings: LocalAiRiskWarning[]
  localAiCommandResult: LocalAiCommandResult | null
  mobileSync: MobileSyncStatus | null
  localRiskCommand: string
  setLocalRiskCommand: (value: string) => void
  loadInvestmentState: (silent?: boolean, force?: boolean) => Promise<void>
  syncOpenMarkets: () => Promise<void>
  syncMissingInfo: () => Promise<void>
  readPortfolioFile: () => Promise<void>
  clearInvestmentData: () => Promise<void>
  sendLocalRiskCommand: () => Promise<void>
  saveMobileSyncRemoteUrl: (remoteUrl: string) => Promise<void>
  rotateMobileSyncPairing: () => Promise<void>
  exportInvestmentReport: () => Promise<void>
  runInvestmentCommand: (
    command: string,
    label: string,
    timeoutMs?: number
  ) => Promise<void>
  runInvestmentV2Command: (
    command: string,
    payload: Record<string, unknown>,
    label: string,
    timeoutMs?: number,
    requireHoldings?: boolean
  ) => Promise<InvestmentResult | null>
}

export function formatInvestmentClock(value?: string): string {
  const timestamp = Date.parse(value || '')
  if (!Number.isFinite(timestamp)) return ''
  return new Date(timestamp).toLocaleString('zh-TW', {
    hour12: false,
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  })
}

export function formatInvestmentNumber(value: unknown, digits = 2): string {
  const number = typeof value === 'number' ? value : Number(value)
  if (!Number.isFinite(number)) return '-'
  return number.toLocaleString('zh-TW', { maximumFractionDigits: digits })
}

const INVESTMENT_CODE_LABELS: Record<string, string> = {
  TW: '台股',
  US: '美股',
  HK: '港股',
  FUND: '共同基金',
  STOCK: '股票',
  ETF: 'ETF',
  BOND: '債券',
  CASH: '現金',
  CRYPTO: '加密資產',
  OTHER: '其他',
  AUTO: '自動判斷',
}

export function investmentCodeLabel(value: unknown, fallback = '-'): string {
  const normalized = String(value ?? '').trim()
  if (!normalized) return fallback
  return INVESTMENT_CODE_LABELS[normalized.toUpperCase()] || normalized
}

export function investmentStatusLabel(status: string): string {
  if (status === 'completed') return '完成'
  if (status === 'running') return '執行中'
  if (status === 'waiting_verification') return '等待驗證'
  if (status === 'failed') return '失敗'
  return '等待'
}

export function investmentRunLabel(run: InvestmentRun): string {
  if (run.role === 'primary_analysis') return `${run.provider} 主要分析`
  if (
    run.role === 'investment_risk_monitor' ||
    run.role === 'local_risk_monitor'
  ) {
    return '投資管家投資監測'
  }
  if (run.role === 'quote_search') return 'Gemini 報價/搜尋'
  if (run.role === 'feature_extract') return 'GPT 特徵萃取'
  return run.role
}

function toNumber(value: unknown, fallback = 0): number {
  const number = typeof value === 'number' ? value : Number(value)
  return Number.isFinite(number) ? number : fallback
}

function ageHours(value?: string): number | null {
  const timestamp = Date.parse(value || '')
  if (!Number.isFinite(timestamp)) return null
  const hours = Math.max(0, (Date.now() - timestamp) / 3_600_000)
  return Math.round(hours * 100) / 100
}

function buildClientInvestmentDiagnostics(
  state: InvestmentState | undefined
): InvestmentDiagnostics | null {
  if (!state) return null

  const holdings = Array.isArray(state.holdings) ? state.holdings : []
  const portfolio = state.portfolio || null
  const workbookQuality = state.workbook_scan_quality || null
  const localAiStatus = state.xingcheng_product_status || null
  const networkContext =
    state.xingcheng_network_context || localAiStatus?.network_context || null
  const riskWarnings = Array.isArray(state.xingcheng_risk_warnings)
    ? state.xingcheng_risk_warnings
    : []
  const runs = Array.isArray(state.ai_runs) ? state.ai_runs : []
  const criticalCount = Math.max(
    toNumber(localAiStatus?.critical_count),
    riskWarnings.filter((item) => item.severity === 'critical').length
  )
  const warningCount = Math.max(
    toNumber(localAiStatus?.warning_count),
    riskWarnings.filter(
      (item) => item.severity === 'critical' || item.severity === 'warning'
    ).length
  )
  const workbookState = workbookQuality?.state || 'not_applicable'
  const localAiState =
    localAiStatus?.state || (holdings.length > 0 ? 'attention' : 'empty')

  let stateKey = 'setup'
  let stateLabel = '等待持股資料'
  let message = '請先讀取持股檔，診斷會在載入後自動更新。'

  if (holdings.length > 0) {
    if (
      criticalCount > 0 ||
      workbookState === 'critical' ||
      localAiState === 'critical'
    ) {
      stateKey = 'critical'
      stateLabel = '需要處理'
      message = `已載入 ${holdings.length} 筆持股，偵測到 ${criticalCount} 項重大風險。`
    } else if (
      warningCount > 0 ||
      workbookState === 'attention' ||
      localAiState === 'attention'
    ) {
      stateKey = 'attention'
      stateLabel = '需要留意'
      message = `已載入 ${holdings.length} 筆持股，診斷資料已由本機狀態同步。`
    } else {
      stateKey = 'ready'
      stateLabel = '診斷完成'
      message = `已載入 ${holdings.length} 筆持股，本地診斷已更新。`
    }
  }

  const portfolioAgeHours = ageHours(portfolio?.imported_at)

  return {
    state: stateKey,
    state_label: stateLabel,
    message,
    generated_at: new Date().toISOString(),
    portfolio: {
      file_name: portfolio?.file_name || '',
      holding_count: holdings.length,
      imported_at: portfolio?.imported_at || '',
      age_hours: portfolioAgeHours,
      stale: (portfolioAgeHours ?? 0) > 72,
    },
    workbook: {
      state: workbookState,
      state_label:
        workbookQuality?.state_label ||
        (holdings.length > 0 ? '已載入' : '尚未掃描'),
      score: workbookQuality?.score ?? null,
      sheet_count: state.workbook_scan?.sheet_count || 0,
      selected_sheet_name: workbookQuality?.selected_sheet_name || '',
      header_row_number: workbookQuality?.header_row_number ?? null,
      header_depth: workbookQuality?.header_depth ?? null,
      valid_data_row_count: workbookQuality?.valid_data_row_count ?? null,
      recommendation: workbookQuality?.recommendation || '',
    },
    xingcheng: {
      state: localAiState,
      state_label:
        localAiStatus?.state_label ||
        (holdings.length > 0 ? '等待投資管家分析' : '等待持股資料'),
      score: localAiStatus?.score ?? null,
      risk_level: localAiStatus?.risk_level,
      risk_level_label: localAiStatus?.risk_level_label,
      watch_status_label: localAiStatus?.watch_status_label,
      network_enabled: Boolean(
        localAiStatus?.network_enabled || networkContext?.enabled
      ),
      network_mode: localAiStatus?.network_mode || networkContext?.mode || '',
      network_mode_label:
        localAiStatus?.network_mode_label || networkContext?.mode_label || '',
      quote_health: localAiStatus?.quote_health || networkContext?.health || '',
      quote_health_label:
        localAiStatus?.quote_health_label || networkContext?.health_label || '',
      network_policy:
        localAiStatus?.network_policy || networkContext?.policy || '',
      quote_provider_count: networkContext?.quote_provider_count || 0,
      quote_providers: networkContext?.quote_providers || [],
      verified_quote_count: networkContext?.verified_quote_count || 0,
      cross_checked_count: networkContext?.cross_checked_count || 0,
      single_source_count: networkContext?.single_source_count || 0,
      untrusted_quote_count: networkContext?.untrusted_quote_count || 0,
      validation_issue_count: networkContext?.validation_issue_count || 0,
      divergence_count: networkContext?.divergence_count || 0,
      stale_quote_count: networkContext?.stale_quote_count || 0,
      symbol_mismatch_count: networkContext?.symbol_mismatch_count || 0,
      currency_mismatch_count: networkContext?.currency_mismatch_count || 0,
      quote_gap_count: networkContext?.quote_gap_count || 0,
      quote_gaps: networkContext?.quote_gaps || [],
      coverage_percent: networkContext?.coverage_percent,
      coverage_label:
        localAiStatus?.coverage_label || networkContext?.coverage_label || '',
      warning_count: warningCount,
      critical_count: criticalCount,
      offline_mode: Boolean(localAiStatus?.offline_mode),
      generated_at: localAiStatus?.generated_at || '',
    },
    runs: {
      count: runs.length,
      latest: runs[0]
        ? {
            run_id: runs[0].run_id,
            role: runs[0].role,
            provider: runs[0].provider,
            status: runs[0].status,
            created_at: runs[0].created_at,
          }
        : {},
    },
    boundaries: {
      local_only: true,
      external_ai: false,
      service_commands: [],
    },
  }
}

function investmentDiagnosticsFromResult(
  result: InvestmentResult
): InvestmentDiagnostics | null {
  return result.diagnostics || buildClientInvestmentDiagnostics(result.state)
}

export async function openInvestmentFile(): Promise<string> {
  const bridge = (window as any).gptBridge as
    | { openFile?: () => Promise<string> }
    | undefined
  if (bridge?.openFile) return await bridge.openFile()
  const electron = (window as any).electron as
    | { invoke?: (channel: string, ...args: unknown[]) => Promise<unknown> }
    | undefined
  const result = await electron?.invoke?.('dialog:open-file')
  if (typeof result === 'string') return result
  const payload = result as { filePaths?: unknown } | undefined
  if (
    Array.isArray(payload?.filePaths) &&
    typeof payload.filePaths[0] === 'string'
  ) {
    return payload.filePaths[0]
  }
  return ''
}

export function useInvestmentWatchFeature({
  request,
  setBusyAction,
  setMessage,
}: InvestmentWatchFeatureOptions): InvestmentWatchFeature {
  const [investmentState, setInvestmentState] = useState<InvestmentState>({})
  const [investmentDiagnostics, setInvestmentDiagnostics] =
    useState<InvestmentDiagnostics | null>(null)
  const [mobileSync, setMobileSync] = useState<MobileSyncStatus | null>(null)
  const [localRiskCommand, setLocalRiskCommand] = useState('')
  const stateRevisionRef = useRef('')
  const stateLoadRef = useRef<Promise<void> | null>(null)
  const marketSyncRef = useRef<Promise<void> | null>(null)
  const stateCircuitRef = useRef({ failures: 0, nextAttemptAt: 0 })
  const marketCircuitRef = useRef({ failures: 0, nextAttemptAt: 0 })
  const missingInfoSyncRef = useRef<Promise<void> | null>(null)
  const stateMutationEpochRef = useRef(0)
  const stateMutationDepthRef = useRef(0)

  const beginStateMutation = useCallback(() => {
    stateMutationDepthRef.current += 1
    stateMutationEpochRef.current += 1
    return stateMutationEpochRef.current
  }, [])

  const finishStateMutation = useCallback(() => {
    stateMutationDepthRef.current = Math.max(0, stateMutationDepthRef.current - 1)
  }, [])

  const applyInvestmentResult = useCallback(
    (
      result: InvestmentResult,
      options: { mutationEpoch?: number; backgroundEpoch?: number } = {}
    ): boolean => {
      if (
        options.mutationEpoch !== undefined &&
        options.mutationEpoch !== stateMutationEpochRef.current
      ) {
        return false
      }
      if (
        options.backgroundEpoch !== undefined &&
        (options.backgroundEpoch !== stateMutationEpochRef.current ||
          stateMutationDepthRef.current > 0)
      ) {
        return false
      }

      if (result.state_revision) stateRevisionRef.current = result.state_revision
      if (result.mobile_sync) setMobileSync(result.mobile_sync)
      if (result.state) {
        setInvestmentState(result.state)
        setInvestmentDiagnostics(investmentDiagnosticsFromResult(result))
      } else if (result.market_sessions) {
        setInvestmentState((current) => ({
          ...current,
          market_sessions: result.market_sessions,
        }))
      }
      return true
    },
    []
  )
  const investmentHoldings = useMemo(
    () => investmentState.holdings || [],
    [investmentState.holdings]
  )
  const investmentRuns = useMemo(
    () => investmentState.ai_runs || [],
    [investmentState.ai_runs]
  )
  const localAiProductStatus = useMemo(
    () => investmentState.xingcheng_product_status || null,
    [investmentState.xingcheng_product_status]
  )
  const localAiRiskWarnings = useMemo(
    () => investmentState.xingcheng_risk_warnings || [],
    [investmentState.xingcheng_risk_warnings]
  )
  const localAiCommandResult = useMemo(
    () => investmentState.xingcheng_command_result || null,
    [investmentState.xingcheng_command_result]
  )

  const loadInvestmentState = useCallback(
    (silent = false, force = false): Promise<void> => {
      if (stateLoadRef.current) return stateLoadRef.current
      if (stateMutationDepthRef.current > 0) return Promise.resolve()
      const circuit = stateCircuitRef.current
      if (silent && !force && Date.now() < circuit.nextAttemptAt) {
        return Promise.resolve()
      }
      const backgroundEpoch = stateMutationEpochRef.current
      const pending = (async () => {
        try {
          const result = (await request(
            'investment_watch_get_state',
            {
              state_revision: force ? '' : stateRevisionRef.current,
              force,
            },
            15000
          )) as InvestmentResult
          if (result.ok === false) {
            throw new Error(String(result.message || '投資看盤載入失敗'))
          }
          stateCircuitRef.current = { failures: 0, nextAttemptAt: 0 }
          applyInvestmentResult(result, { backgroundEpoch })
        } catch (error) {
          const failures = stateCircuitRef.current.failures + 1
          const delay =
            failures < 3 ? 0 : Math.min(60_000, 5_000 * 2 ** (failures - 3))
          stateCircuitRef.current = {
            failures,
            nextAttemptAt: Date.now() + delay,
          }
          if (!silent) {
            setMessage(
              error instanceof Error ? error.message : '投資看盤載入失敗'
            )
          }
        }
      })()
      stateLoadRef.current = pending
      void pending.finally(() => {
        if (stateLoadRef.current === pending) stateLoadRef.current = null
      })
      return pending
    },
    [applyInvestmentResult, request, setMessage]
  )

  const syncOpenMarkets = useCallback((): Promise<void> => {
    if (marketSyncRef.current) return marketSyncRef.current
    if (stateMutationDepthRef.current > 0) return Promise.resolve()
    const circuit = marketCircuitRef.current
    if (Date.now() < circuit.nextAttemptAt) return Promise.resolve()
    const backgroundEpoch = stateMutationEpochRef.current
    const pending = (async () => {
      try {
        const result = (await request(
          'investment_watch_sync_open_markets',
          {},
          180000
        )) as InvestmentResult
        if (result.ok === false) {
          throw new Error(String(result.message || '市場同步失敗'))
        }
        marketCircuitRef.current = { failures: 0, nextAttemptAt: 0 }
        applyInvestmentResult(result, { backgroundEpoch })
      } catch {
        const failures = marketCircuitRef.current.failures + 1
        marketCircuitRef.current = {
          failures,
          nextAttemptAt:
            Date.now() +
            Math.min(5 * 60_000, 60_000 * 2 ** Math.max(0, failures - 1)),
        }
        // Automatic market refresh is best effort; backend errors are logged locally.
      }
    })()
    marketSyncRef.current = pending
    void pending.finally(() => {
      if (marketSyncRef.current === pending) marketSyncRef.current = null
    })
    return pending
  }, [applyInvestmentResult, request])

  const syncMissingInfo = useCallback((): Promise<void> => {
    if (missingInfoSyncRef.current) return missingInfoSyncRef.current
    const mutationEpoch = beginStateMutation()
    const pending = (async () => {
      try {
        const result = (await request(
          'investment_watch_sync_dividends',
          {},
          600000
        )) as InvestmentResult
        if (result.ok === false || !result.state) return
        applyInvestmentResult(result, { mutationEpoch })
      } catch {
        // Missing public data remains unchanged and can be retried manually.
      } finally {
        finishStateMutation()
      }
    })()
    missingInfoSyncRef.current = pending
    void pending.finally(() => {
      if (missingInfoSyncRef.current === pending) {
        missingInfoSyncRef.current = null
      }
    })
    return pending
  }, [applyInvestmentResult, beginStateMutation, finishStateMutation, request])

  const readPortfolioFile = useCallback(async () => {
    setBusyAction('investment:read-file')
    const mutationEpoch = beginStateMutation()
    try {
      const file = await openInvestmentFile()
      if (!file) {
        setMessage('未選擇要讀取的持股檔案。')
        return
      }
      const result = (await request(
        'investment_watch_read_portfolio_file',
        { path: file },
        240000
      )) as InvestmentResult
      if (result.ok === false)
        throw new Error(String(result.message || '讀取失敗'))
      applyInvestmentResult(result, { mutationEpoch })
      setMessage(String(result.message || '持股檔案已讀取'))
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '讀取失敗')
    } finally {
      finishStateMutation()
      setBusyAction('')
    }
  }, [
    applyInvestmentResult,
    beginStateMutation,
    finishStateMutation,
    request,
    setBusyAction,
    setMessage,
  ])

  const clearInvestmentData = useCallback(async () => {
    setBusyAction('investment:clear')
    const mutationEpoch = beginStateMutation()
    try {
      const result = (await request(
        'investment_watch_clear_state',
        { confirmed: true },
        30000
      )) as InvestmentResult
      if (result.ok === false)
        throw new Error(String(result.message || '刪除失敗'))
      applyInvestmentResult(result, { mutationEpoch })
      setMessage(String(result.message || '投資看盤舊資料已刪除'))
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '刪除失敗')
    } finally {
      finishStateMutation()
      setBusyAction('')
    }
  }, [
    applyInvestmentResult,
    beginStateMutation,
    finishStateMutation,
    request,
    setBusyAction,
    setMessage,
  ])

  const runInvestmentCommand = useCallback(
    async (command: string, label: string, timeoutMs = 240000) => {
      if (investmentHoldings.length === 0) {
        setMessage('請先讀取 Excel 持股檔。')
        return
      }
      setBusyAction(`investment:${command}`)
      setMessage(`${label}執行中...`)
      const mutationEpoch = beginStateMutation()
      try {
        const result = (await request(
          command,
          {},
          timeoutMs
        )) as InvestmentResult
        if (result.ok === false) {
          throw new Error(String(result.message || `${label}失敗`))
        }
        applyInvestmentResult(result, { mutationEpoch })
        setMessage(String(result.message || `${label}完成`))
      } catch (error) {
        setMessage(error instanceof Error ? error.message : `${label}失敗`)
      } finally {
        finishStateMutation()
        setBusyAction('')
      }
    },
    [
      applyInvestmentResult,
      beginStateMutation,
      finishStateMutation,
      investmentHoldings.length,
      request,
      setBusyAction,
      setMessage,
    ]
  )

  const runInvestmentV2Command = useCallback(
    async (
      command: string,
      payload: Record<string, unknown>,
      label: string,
      timeoutMs = 120000,
      requireHoldings = false
    ): Promise<InvestmentResult | null> => {
      if (requireHoldings && investmentHoldings.length === 0) {
        setMessage('請先讀取持股檔。')
        return null
      }
      setBusyAction(`investment:${command}`)
      setMessage(`${label}執行中...`)
      const mutationEpoch = beginStateMutation()
      try {
        let result = (await request(
          command,
          payload,
          timeoutMs
        )) as InvestmentResult
        const pollingDeadline = Date.now() + timeoutMs
        while (
          command === 'investment_watch_import_excel_mapping' &&
          result.processing === true &&
          typeof result.operation_id === 'string' &&
          result.operation_id
        ) {
          const remainingMs = pollingDeadline - Date.now()
          if (remainingMs <= 0) {
            setMessage(
              `${label}仍在後端安全處理（operation_id: ${result.operation_id}），重開程式後也會繼續。`
            )
            return result
          }
          const pollAfterMs =
            typeof result.poll_after_ms === 'number'
              ? Math.max(250, Math.min(3000, result.poll_after_ms))
              : 750
          setMessage(`${label}後端處理中...`)
          await new Promise<void>((resolve) => {
            window.setTimeout(resolve, Math.min(pollAfterMs, remainingMs))
          })
          result = (await request(
            command,
            {
              operation_id: result.operation_id,
              poll: true,
            },
            Math.max(1000, Math.min(30000, remainingMs))
          )) as InvestmentResult
        }
        if (result.ok === false) {
          throw new Error(String(result.message || `${label}失敗`))
        }
        applyInvestmentResult(result, { mutationEpoch })
        setMessage(String(result.message || `${label}完成`))
        return result
      } catch (error) {
        setMessage(error instanceof Error ? error.message : `${label}失敗`)
        return null
      } finally {
        finishStateMutation()
        setBusyAction('')
      }
    },
    [
      applyInvestmentResult,
      beginStateMutation,
      finishStateMutation,
      investmentHoldings.length,
      request,
      setBusyAction,
      setMessage,
    ]
  )

  const sendLocalRiskCommand = useCallback(async () => {
    const instruction = localRiskCommand.trim()
    if (investmentHoldings.length === 0) {
      setMessage('請先讀取 Excel 持股檔。')
      return
    }
    if (!instruction) {
      setMessage('請輸入投資管家命令。')
      return
    }
    setBusyAction('investment:local-risk-command')
    setMessage('投資管家正在分析並交由 ChatGPT 最終統籌...')
    const mutationEpoch = beginStateMutation()
    try {
      const result = (await request(
        'investment_watch_run_local_risk_ai',
        { instruction },
        240000
      )) as InvestmentResult
      if (result.ok === false) {
        throw new Error(String(result.message || '投資管家命令失敗'))
      }
      applyInvestmentResult(result, { mutationEpoch })
      setLocalRiskCommand('')
      setMessage(String(result.message || '投資管家命令完成'))
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '投資管家命令失敗')
    } finally {
      finishStateMutation()
      setBusyAction('')
    }
  }, [
    applyInvestmentResult,
    beginStateMutation,
    finishStateMutation,
    investmentHoldings.length,
    localRiskCommand,
    request,
    setBusyAction,
    setMessage,
  ])

  const saveMobileSyncRemoteUrl = useCallback(
    async (remoteUrl: string) => {
      setBusyAction('investment:mobile-sync')
      const mutationEpoch = beginStateMutation()
      try {
        const result = (await request(
          'investment_watch_set_mobile_sync_remote_url',
          { remote_url: remoteUrl },
          30000
        )) as InvestmentResult
        if (result.ok === false) {
          throw new Error(String(result.message || '手機同步橋接更新失敗'))
        }
        applyInvestmentResult(result, { mutationEpoch })
        setMessage(String(result.message || '手機同步橋接已更新'))
      } catch (error) {
        setMessage(
          error instanceof Error ? error.message : '手機同步橋接更新失敗'
        )
      } finally {
        finishStateMutation()
        setBusyAction('')
      }
    },
    [
      applyInvestmentResult,
      beginStateMutation,
      finishStateMutation,
      request,
      setBusyAction,
      setMessage,
    ]
  )

  const rotateMobileSyncPairing = useCallback(async () => {
    setBusyAction('investment:mobile-sync-pairing')
    const mutationEpoch = beginStateMutation()
    try {
      const result = (await request(
        'investment_watch_rotate_mobile_sync_pairing',
        {},
        30000
      )) as InvestmentResult
      if (result.ok === false) {
        throw new Error(String(result.message || '手機同步配對碼更新失敗'))
      }
      applyInvestmentResult(result, { mutationEpoch })
      setMessage(String(result.message || '手機同步配對碼已更新'))
    } catch (error) {
      setMessage(
        error instanceof Error ? error.message : '手機同步配對碼更新失敗'
      )
    } finally {
      finishStateMutation()
      setBusyAction('')
    }
  }, [
    applyInvestmentResult,
    beginStateMutation,
    finishStateMutation,
    request,
    setBusyAction,
    setMessage,
  ])

  const exportInvestmentReport = useCallback(async () => {
    setBusyAction('investment:export-report')
    const mutationEpoch = beginStateMutation()
    try {
      const result = (await request(
        'investment_watch_export_report',
        {},
        30000
      )) as InvestmentResult
      if (result.ok === false) {
        throw new Error(String(result.message || '診斷報告匯出失敗'))
      }
      applyInvestmentResult(result, { mutationEpoch })
      setMessage(
        String(result.message || `診斷報告已匯出：${result.report_path || ''}`)
      )
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '診斷報告匯出失敗')
    } finally {
      finishStateMutation()
      setBusyAction('')
    }
  }, [
    applyInvestmentResult,
    beginStateMutation,
    finishStateMutation,
    request,
    setBusyAction,
    setMessage,
  ])

  return {
    investmentState,
    investmentDiagnostics,
    investmentHoldings,
    investmentRuns,
    localAiProductStatus,
    localAiRiskWarnings,
    localAiCommandResult,
    mobileSync,
    localRiskCommand,
    setLocalRiskCommand,
    loadInvestmentState,
    syncOpenMarkets,
    syncMissingInfo,
    readPortfolioFile,
    clearInvestmentData,
    sendLocalRiskCommand,
    saveMobileSyncRemoteUrl,
    rotateMobileSyncPairing,
    exportInvestmentReport,
    runInvestmentCommand,
    runInvestmentV2Command,
  }
}
