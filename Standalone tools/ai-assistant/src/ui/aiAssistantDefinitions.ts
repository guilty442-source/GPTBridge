import type { ExcelHorizontalSheetPreview } from './investmentWatchFeature'

export type InvestmentShellState = {
  ok?: boolean
  message?: string
  tool_root?: string
  state_path?: string
  local_only?: boolean
}

export type HoldingDraft = {
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
  dividend_frequency: string
  payback_rate_percent: string
  current_value_twd: string
  fund_code: string
  fund_isin: string
  fund_share_class: string
  fund_quote_symbol: string
}

export const EMPTY_HOLDING_DRAFT: HoldingDraft = {
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
  dividend_frequency: 'unknown',
  payback_rate_percent: '',
  current_value_twd: '',
  fund_code: '',
  fund_isin: '',
  fund_share_class: '',
  fund_quote_symbol: '',
}

export const EXCEL_MAPPING_FIELDS = [
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

export const CONSOLIDATED_ROW_FIELDS = [
  { key: 'fund_start_row_number', label: '基金起始列' },
  { key: 'security_start_row_number', label: '證券起始列' },
  { key: 'data_end_row_number', label: '資料結束列' },
] as const

export const CONSOLIDATED_COLUMN_FIELDS = [
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

export const EXCEL_COLUMN_OPTIONS = Array.from({ length: 96 }, (_, index) => index)

export type ExcelColumnMapping = Record<string, string>
export type ExcelLayoutMode = 'consolidated_report' | 'horizontal_matrix' | 'row_mapping'

export type ExcelHorizontalSheetDraft = {
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

export type ExcelConsolidatedDraft = {
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

export const EMPTY_CONSOLIDATED_DRAFT: ExcelConsolidatedDraft = {
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

export function excelColumnLetter(index: number): string {
  let value = index + 1
  let letters = ''
  while (value > 0) {
    value -= 1
    letters = String.fromCharCode(65 + (value % 26)) + letters
    value = Math.floor(value / 26)
  }
  return letters
}

export function socketStatusLabel(status: string): string {
  if (status === 'Connected') return '已連線'
  if (status === 'Connecting') return '連線中'
  if (status === 'Disconnected') return '未連線'
  if (status === 'Error') return '連線錯誤'
  return status
}

export function waitForIpcEvent<T = Record<string, unknown>>(
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
      const detail = (event as CustomEvent).detail || {}
      const payload = (detail.payload || {}) as Record<string, unknown>
      if (detail.event !== eventName) return
      if (requestId && String(payload.request_id || '') !== requestId) return
      cleanup()
      resolve(payload as T)
    }
    const connectionHandler = (event: Event) => {
      if ((event as CustomEvent).detail?.connected !== false) return
      cleanup()
      reject(new Error('後端連線已中斷，操作結果未知；重新連線後請重新載入狀態'))
    }
    timer = window.setTimeout(() => {
      cleanup()
      reject(new Error(`等待 ${eventName} 逾時`))
    }, timeoutMs)
    window.addEventListener('ipc_event', handler)
    window.addEventListener('socket_connected', connectionHandler)
  })
}

export type WorkspaceView = 'portfolio' | 'browser' | 'accounting' | 'system'

export const WORKSPACE_VIEWS: Array<{ key: WorkspaceView; label: string }> = [
  { key: 'portfolio', label: '持股' },
  { key: 'browser', label: '瀏覽器' },
  { key: 'accounting', label: 'AI帳務' },
  { key: 'system', label: '系統' },
]
