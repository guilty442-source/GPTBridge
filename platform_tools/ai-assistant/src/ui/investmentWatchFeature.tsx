import { useCallback, useMemo, useState } from 'react'

type BackendRequest = (
  command: string,
  payload?: Record<string, unknown>,
  timeoutMs?: number
) => Promise<Record<string, unknown>>

type SetMessage = (message: string) => void
type SetBusyAction = (action: string) => void

export type InvestmentHolding = {
  symbol?: string
  name?: string
  market?: string
  quantity?: number
  average_cost?: number | null
  currency?: string
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
  recommendation?: string
  next_actions?: string[]
  command_suggestions?: string[]
  watch_status?: string
  watch_status_label?: string
  offline_mode?: boolean
  has_portfolio?: boolean
  portfolio_count?: number
  warning_count?: number
  critical_count?: number
  coverage_label?: string
  generated_at?: string
}

export type LocalAiRiskWarning = {
  severity?: string
  code?: string
  title?: string
  detail?: string
  action?: string
  symbol?: string
  metric?: number
}

export type LocalAiCommandResult = {
  intent?: string
  sections?: string[]
  symbols?: string[]
  matched_symbols?: string[]
  missing_symbols?: string[]
  portfolio_score?: number
  next_actions?: string[]
  text?: string
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
  header_row_number?: number
  header_mode?: string
  score?: number
  valid_data_row_count?: number
  usable?: boolean
}

export type WorkbookScanSummary = {
  sheet_count?: number
  selected_sheet?: WorkbookSheetScan | null
  sheets?: WorkbookSheetScan[]
}

export type InvestmentState = {
  portfolio?: {
    file_name?: string
    holding_count?: number
    imported_at?: string
  } | null
  holdings?: InvestmentHolding[]
  ai_runs?: InvestmentRun[]
  workbook_scan?: WorkbookScanSummary | null
  workbook_scan_quality?: WorkbookScanQuality | null
  local_ai_product_status?: LocalAiProductStatus | null
  local_ai_risk_warnings?: LocalAiRiskWarning[]
  local_ai_command_result?: LocalAiCommandResult | null
  portfolio_memory?: string
  shared_memory?: string
}

type InvestmentResult = {
  ok?: boolean
  message?: string
  state?: InvestmentState
}

type InvestmentWatchFeatureOptions = {
  request: BackendRequest
  setMessage: SetMessage
  setBusyAction: SetBusyAction
}

export type InvestmentWatchFeature = {
  investmentState: InvestmentState
  investmentHoldings: InvestmentHolding[]
  investmentRuns: InvestmentRun[]
  localAiProductStatus: LocalAiProductStatus | null
  localAiRiskWarnings: LocalAiRiskWarning[]
  localAiCommandResult: LocalAiCommandResult | null
  localRiskCommand: string
  setLocalRiskCommand: (value: string) => void
  loadInvestmentState: (silent?: boolean) => Promise<void>
  importPortfolio: () => Promise<void>
  clearInvestmentData: () => Promise<void>
  sendLocalRiskCommand: () => Promise<void>
  runInvestmentCommand: (
    command: string,
    label: string,
    timeoutMs?: number
  ) => Promise<void>
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

export function investmentStatusLabel(status: string): string {
  if (status === 'completed') return '完成'
  if (status === 'running') return '執行中'
  if (status === 'waiting_verification') return '等待驗證'
  if (status === 'failed') return '失敗'
  return '等待'
}

export function investmentRunLabel(run: InvestmentRun): string {
  if (run.role === 'primary_analysis') return `${run.provider} 主要分析`
  if (run.role === 'local_risk_monitor') return '本地輔助AI'
  if (run.role === 'quote_search') return 'Gemini 報價/搜尋'
  if (run.role === 'feature_extract') return 'GPT 特徵萃取'
  return run.role
}

async function openPortfolioFile(): Promise<string> {
  if (window.gptBridge?.openFile) return await window.gptBridge.openFile()
  const result = await window.electron?.invoke('dialog:open-file')
  if (typeof result === 'string') return result
  const payload = result as { filePaths?: unknown } | undefined
  if (Array.isArray(payload?.filePaths) && typeof payload.filePaths[0] === 'string') {
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
  const [localRiskCommand, setLocalRiskCommand] = useState('')
  const investmentHoldings = useMemo(
    () => investmentState.holdings || [],
    [investmentState.holdings]
  )
  const investmentRuns = useMemo(
    () => investmentState.ai_runs || [],
    [investmentState.ai_runs]
  )
  const localAiProductStatus = useMemo(
    () => investmentState.local_ai_product_status || null,
    [investmentState.local_ai_product_status]
  )
  const localAiRiskWarnings = useMemo(
    () => investmentState.local_ai_risk_warnings || [],
    [investmentState.local_ai_risk_warnings]
  )
  const localAiCommandResult = useMemo(
    () => investmentState.local_ai_command_result || null,
    [investmentState.local_ai_command_result]
  )

  const loadInvestmentState = useCallback(
    async (silent = false) => {
      try {
        const result = (await request(
          'investment_watch_get_state',
          {},
          15000
        )) as InvestmentResult
        if (result.ok === false) {
          throw new Error(String(result.message || '投資看盤載入失敗'))
        }
        setInvestmentState(result.state || {})
      } catch (error) {
        if (!silent) {
          setMessage(error instanceof Error ? error.message : '投資看盤載入失敗')
        }
      }
    },
    [request, setMessage]
  )

  const importPortfolio = useCallback(async () => {
    setBusyAction('investment:import')
    try {
      const file = await openPortfolioFile()
      if (!file) {
        setMessage('未選擇 Excel 持股檔。')
        return
      }
      const result = (await request(
        'investment_watch_import_portfolio',
        { path: file },
        240000
      )) as InvestmentResult
      if (result.ok === false) throw new Error(String(result.message || '匯入失敗'))
      setInvestmentState(result.state || {})
      setMessage(String(result.message || '持股已更新'))
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '匯入失敗')
    } finally {
      setBusyAction('')
    }
  }, [request, setBusyAction, setMessage])

  const clearInvestmentData = useCallback(async () => {
    setBusyAction('investment:clear')
    try {
      const result = (await request(
        'investment_watch_clear_state',
        {},
        30000
      )) as InvestmentResult
      if (result.ok === false) throw new Error(String(result.message || '刪除失敗'))
      setInvestmentState(result.state || {})
      setMessage(String(result.message || '投資看盤舊資料已刪除'))
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '刪除失敗')
    } finally {
      setBusyAction('')
    }
  }, [request, setBusyAction, setMessage])

  const runInvestmentCommand = useCallback(
    async (command: string, label: string, timeoutMs = 240000) => {
      if (investmentHoldings.length === 0) {
        setMessage('請先匯入 Excel 持股檔。')
        return
      }
      setBusyAction(`investment:${command}`)
      setMessage(`${label}執行中...`)
      try {
        const result = (await request(command, {}, timeoutMs)) as InvestmentResult
        if (result.ok === false) {
          throw new Error(String(result.message || `${label}失敗`))
        }
        setInvestmentState(result.state || {})
        setMessage(String(result.message || `${label}完成`))
      } catch (error) {
        setMessage(error instanceof Error ? error.message : `${label}失敗`)
      } finally {
        setBusyAction('')
      }
    },
    [investmentHoldings.length, request, setBusyAction, setMessage]
  )

  const sendLocalRiskCommand = useCallback(async () => {
    const instruction = localRiskCommand.trim()
    if (investmentHoldings.length === 0) {
      setMessage('請先匯入 Excel 持股檔。')
      return
    }
    if (!instruction) {
      setMessage('請輸入本地輔助AI命令。')
      return
    }
    setBusyAction('investment:local-risk-command')
    setMessage('本地輔助AI執行命令中...')
    try {
      const result = (await request(
        'investment_watch_run_local_risk_ai',
        { instruction },
        240000
      )) as InvestmentResult
      if (result.ok === false) {
        throw new Error(String(result.message || '本地輔助AI命令失敗'))
      }
      setInvestmentState(result.state || {})
      setLocalRiskCommand('')
      setMessage(String(result.message || '本地輔助AI命令完成'))
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '本地輔助AI命令失敗')
    } finally {
      setBusyAction('')
    }
  }, [
    investmentHoldings.length,
    localRiskCommand,
    request,
    setBusyAction,
    setMessage,
  ])

  return {
    investmentState,
    investmentHoldings,
    investmentRuns,
    localAiProductStatus,
    localAiRiskWarnings,
    localAiCommandResult,
    localRiskCommand,
    setLocalRiskCommand,
    loadInvestmentState,
    importPortfolio,
    clearInvestmentData,
    sendLocalRiskCommand,
    runInvestmentCommand,
  }
}
