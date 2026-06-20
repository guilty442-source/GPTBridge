import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useLocalBackendSocket } from './backendSocket'
import {
  formatInvestmentClock,
  formatInvestmentNumber,
  investmentRunLabel,
  investmentStatusLabel,
  useInvestmentWatchFeature,
} from './investmentWatchFeature'
import './ai-assistant.css'

type Agent = {
  agent_id: string
  name: string
  provider: string
  home_url: string
  enabled: number
  selected: number
  status: string
  last_error: string
}

type AgentResponse = {
  response_id: string
  message_id: string
  agent_id: string
  status: string
  content: string
  error: string
  created_at: string
  updated_at: string
}

type GroupMessage = {
  message_id: string
  role: string
  content: string
  selected_agents: string[]
  created_at: string
  responses: AgentResponse[]
}

type MemoryItem = {
  memory_id: string
  kind: string
  title: string
  content: string
}

type NexusState = {
  ok?: boolean
  message?: string
  agents?: Agent[]
  messages?: GroupMessage[]
  memory_items?: MemoryItem[]
  database_path?: string
  workspace_path?: string
  browser_profile_path?: string
  safety_notice?: string
}

function formatClock(value: string): string {
  const timestamp = Date.parse(value)
  if (!Number.isFinite(timestamp)) return ''
  return new Date(timestamp).toLocaleString('zh-TW', {
    hour12: false,
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  })
}

function responseLabel(status: string): string {
  if (status === 'completed') return '完成'
  if (status === 'running') return '執行中'
  if (status === 'waiting_verification') return '等待驗證'
  if (status === 'failed') return '失敗'
  return '等待'
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
    const handler = (event: Event) => {
      const customEvent = event as CustomEvent
      const detail = customEvent.detail || {}
      const payload = (detail.payload || {}) as Record<string, unknown>
      if (detail.event !== eventName) return
      if (requestId && String(payload.request_id || '') !== requestId) return
      window.clearTimeout(timer)
      window.removeEventListener('ipc_event', handler)
      resolve(payload as T)
    }
    timer = window.setTimeout(() => {
      window.removeEventListener('ipc_event', handler)
      reject(new Error(`等待 ${eventName} 逾時`))
    }, timeoutMs)
    window.addEventListener('ipc_event', handler)
  })
}

export function AiAssistantWindowApp() {
  const { sendCommand, status: socketStatus } = useLocalBackendSocket()
  const [agents, setAgents] = useState<Agent[]>([])
  const [messages, setMessages] = useState<GroupMessage[]>([])
  const [memoryItems, setMemoryItems] = useState<MemoryItem[]>([])
  const [selectedAgents, setSelectedAgents] = useState<Set<string>>(new Set())
  const [agentListCollapsed, setAgentListCollapsed] = useState(true)
  const [draft, setDraft] = useState('')
  const [memoryDraft, setMemoryDraft] = useState('')
  const [message, setMessage] = useState('AI投資管家已就緒')
  const [busyAction, setBusyAction] = useState('')
  const [paths, setPaths] = useState({ workspace: '', database: '', profile: '' })
  const [safetyNotice, setSafetyNotice] = useState('')
  const loadedSelectionRef = useRef(false)

  const selectedAgentList = useMemo(
    () => agents.filter((agent) => selectedAgents.has(agent.agent_id)),
    [agents, selectedAgents]
  )
  const agentsById = useMemo(
    () => new Map(agents.map((agent) => [agent.agent_id, agent])),
    [agents]
  )

  const request = useCallback(
    async (
      command: string,
      payload: Record<string, unknown> = {},
      timeoutMs = 30000
    ) => {
      const requestId = `${command}:${Date.now()}:${Math.random().toString(16).slice(2)}`
      const waitPromise = waitForIpcEvent<Record<string, unknown>>(
        `${command}_result`,
        timeoutMs,
        requestId
      )
      const sent = sendCommand(command, { ...payload, request_id: requestId })
      if (!sent.ok && !sent.queued) {
        throw new Error(sent.message || '後端尚未接收指令')
      }
      const response = await waitPromise
      const maybeMemoryItems = (response as NexusState).memory_items
      if (Array.isArray(maybeMemoryItems)) {
        setMemoryItems(maybeMemoryItems)
      }
      return response
    },
    [sendCommand]
  )

  const applyState = useCallback((state: NexusState) => {
    const nextAgents = Array.isArray(state.agents) ? state.agents : []
    setAgents(nextAgents)
    setMessages(Array.isArray(state.messages) ? state.messages : [])
    setMemoryItems(Array.isArray(state.memory_items) ? state.memory_items : [])
    setPaths({
      workspace: String(state.workspace_path || ''),
      database: String(state.database_path || ''),
      profile: String(state.browser_profile_path || ''),
    })
    setSafetyNotice(String(state.safety_notice || ''))
    if (!loadedSelectionRef.current && nextAgents.length > 0) {
      loadedSelectionRef.current = true
      setSelectedAgents(
        new Set(
          nextAgents
            .filter((agent) => Number(agent.selected) === 1)
            .map((agent) => agent.agent_id)
        )
      )
    }
  }, [])

  const loadState = useCallback(
    async (silent = false) => {
      try {
        const result = (await request('ai_nexus_get_state', {}, 15000)) as NexusState
        if (result.ok === false) throw new Error(String(result.message || '載入失敗'))
        applyState(result)
        if (!silent) setMessage('AI投資管家已載入')
      } catch (error) {
        if (!silent) {
          setMessage(error instanceof Error ? error.message : '載入 AI投資管家失敗')
        }
      }
    },
    [applyState, request]
  )

  const investmentWatch = useInvestmentWatchFeature({
    request,
    setBusyAction,
    setMessage,
  })
  const { loadInvestmentState } = investmentWatch
  const portfolio = investmentWatch.investmentState.portfolio
  const visibleHoldings = investmentWatch.investmentHoldings.slice(0, 30)
  const hiddenHoldingCount = Math.max(0, investmentWatch.investmentHoldings.length - visibleHoldings.length)
  const latestInvestmentRuns = investmentWatch.investmentRuns.slice(0, 5)
  const portfolioUpdatedAt = formatInvestmentClock(portfolio?.imported_at) || '未更新'
  const selectedAgentSummary = `${selectedAgentList.length} / ${agents.length}`
  const selectedAgentNames = selectedAgentList.map((agent) => agent.name).join('、')
  const socketLabel = socketStatusLabel(socketStatus)
  const workbookScan = investmentWatch.investmentState.workbook_scan
  const workbookQuality = investmentWatch.investmentState.workbook_scan_quality
  const selectedWorkbookSheet = workbookScan?.selected_sheet || null
  const workbookScanLabel = selectedWorkbookSheet?.sheet_name
    ? `${selectedWorkbookSheet.sheet_name} · 第 ${selectedWorkbookSheet.header_row_number || '-'} 列`
    : workbookScan?.sheet_count
      ? `已掃描 ${workbookScan.sheet_count} 個工作表`
      : '未掃描'
  const localAiStatus = investmentWatch.localAiProductStatus
  const localAiStatusLabel = localAiStatus?.state_label || (
    investmentWatch.investmentHoldings.length > 0 ? '等待監測' : '待匯入'
  )
  const localAiScore = typeof localAiStatus?.score === 'number' ? String(localAiStatus.score) : '-'
  const localAiActions = (localAiStatus?.next_actions || []).filter(Boolean).slice(0, 3)
  const localAiCommands = (localAiStatus?.command_suggestions || []).filter(Boolean).slice(0, 6)
  const localAiWarnings = investmentWatch.localAiRiskWarnings.slice(0, 6)
  const localAiCommandResult = investmentWatch.localAiCommandResult
  const localAiCommandPreview = (localAiCommandResult?.text || '').trim()
  const localAiRecommendation = localAiStatus?.recommendation || (
    investmentWatch.investmentHoldings.length > 0
      ? '本地AI會在匯入後自動監測，也可直接輸入命令。'
      : '上傳 Excel 後自動啟動本地AI。'
  )

  useEffect(() => {
    void loadState()
    void loadInvestmentState(true)
    const timer = window.setInterval(() => {
      void loadState(true)
      void loadInvestmentState(true)
    }, 5000)
    return () => window.clearInterval(timer)
  }, [loadInvestmentState, loadState])

  const toggleAgent = async (agentId: string) => {
    const next = new Set(selectedAgents)
    if (next.has(agentId)) next.delete(agentId)
    else next.add(agentId)
    setSelectedAgents(next)
    try {
      const result = (await request('ai_nexus_set_agent_selection', {
        agent_ids: Array.from(next),
      })) as NexusState
      if (Array.isArray(result.agents)) setAgents(result.agents)
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '儲存 AI 選擇失敗')
    }
  }

  const openAgent = async (agentId: string) => {
    setBusyAction(`open:${agentId}`)
    try {
      const result = await request('ai_nexus_open_agent', { agent_id: agentId }, 30000)
      if (result.ok === false) throw new Error(String(result.message || '開啟失敗'))
      setMessage(`${agentId} 已在 Microsoft Edge 開啟`)
      await loadState(true)
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '開啟 AI 視窗失敗')
    } finally {
      setBusyAction('')
    }
  }

  const sendGroupMessage = async () => {
    if (!draft.trim()) {
      setMessage('請先輸入問題。')
      return
    }
    if (selectedAgents.size === 0) {
      setMessage('請至少選擇一個 AI。')
      return
    }
    setBusyAction('send')
    setMessage(`正在同步送給 ${selectedAgents.size} 個 AI...`)
    try {
      const result = (await request(
        'ai_nexus_send_message',
        {
          content: draft,
          agent_ids: Array.from(selectedAgents),
        },
        180000
      )) as NexusState
      if (result.ok === false) throw new Error(String(result.message || '送出失敗'))
      setDraft('')
      if (Array.isArray(result.messages)) setMessages(result.messages)
      if (Array.isArray(result.agents)) setAgents(result.agents)
      setMessage(String(result.message || '已收集 AI 回覆'))
    } catch (error) {
      setMessage(error instanceof Error ? error.message : 'AI投資管家送出失敗')
    } finally {
      setBusyAction('')
    }
  }

  const saveMemory = async () => {
    if (!memoryDraft.trim()) {
      setMessage('請輸入共享記憶內容。')
      return
    }
    setBusyAction('memory')
    try {
      const result = (await request('ai_nexus_add_memory', {
        kind: 'note',
        content: memoryDraft,
      })) as NexusState
      if (result.ok === false) throw new Error(String(result.message || '儲存失敗'))
      setMemoryDraft('')
      if (Array.isArray(result.memory_items)) setMemoryItems(result.memory_items)
      setMessage('已儲存共享記憶')
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '儲存共享記憶失敗')
    } finally {
      setBusyAction('')
    }
  }

  return (
    <main className="nexus-app">
      <header className="nexus-topbar">
        <div className="nexus-title-block">
          <p className="nexus-eyebrow">工作台</p>
          <h1>AI投資管家</h1>
          <div className="nexus-top-meta">
            <span>{socketLabel}</span>
            <span>{selectedAgentSummary} AI</span>
            <span>{investmentWatch.investmentHoldings.length} 持股</span>
          </div>
        </div>
        <div className="nexus-toolbar" aria-label="主要操作">
          <button
            type="button"
            className="nexus-primary"
            onClick={() => void investmentWatch.importPortfolio()}
            disabled={Boolean(busyAction)}
          >
            {busyAction === 'investment:import' ? '匯入中...' : '上傳 Excel'}
          </button>
          <button
            type="button"
            onClick={() =>
              void investmentWatch.runInvestmentCommand(
                'investment_watch_run_all_primary_agents',
                '全部主要 AI',
                600000
              )
            }
            disabled={Boolean(busyAction)}
          >
            全部主要 AI
          </button>
          <button
            type="button"
            onClick={() => {
              void loadState()
              void loadInvestmentState()
            }}
            disabled={Boolean(busyAction)}
          >
            重新整理
          </button>
          <button
            type="button"
            className="nexus-danger"
            onClick={() => void investmentWatch.clearInvestmentData()}
            disabled={Boolean(busyAction)}
          >
            {busyAction === 'investment:clear' ? '刪除中...' : '刪除舊資料'}
          </button>
        </div>
      </header>

      <section className="nexus-status-strip" aria-label="工作台狀態">
        <div>
          <span>檔案</span>
          <strong>{portfolio?.file_name || '未匯入'}</strong>
        </div>
        <div>
          <span>掃描</span>
          <strong>{workbookScanLabel}</strong>
        </div>
        <div>
          <span>更新</span>
          <strong>{portfolioUpdatedAt}</strong>
        </div>
        <div>
          <span>訊息</span>
          <strong>{messages.length}</strong>
        </div>
        <div>
          <span>本地AI</span>
          <strong>{localAiStatusLabel} · {localAiScore}</strong>
        </div>
        <div className="nexus-status-message">
          <span>狀態</span>
          <strong>{message}</strong>
        </div>
      </section>

      <section className="nexus-workbench">
        <aside className="nexus-column nexus-column--left">
          <section className="nexus-surface nexus-agent-panel">
            <div className="nexus-section-head nexus-section-head--button">
              <div>
                <span>AI 名單</span>
                <strong>{selectedAgentSummary}</strong>
              </div>
              <button
                type="button"
                className="nexus-toggle-button"
                onClick={() => setAgentListCollapsed((value) => !value)}
                aria-expanded={!agentListCollapsed}
              >
                {agentListCollapsed ? '展開' : '收合'}
              </button>
            </div>
            {agentListCollapsed ? (
              <p className="nexus-agent-summary">
                {selectedAgentNames || '尚未選取 AI'}
              </p>
            ) : (
              <div className="nexus-agent-list">
                {agents.map((agent) => (
                  <article key={agent.agent_id} className="nexus-agent-row">
                    <label>
                      <input
                        type="checkbox"
                        checked={selectedAgents.has(agent.agent_id)}
                        onChange={() => void toggleAgent(agent.agent_id)}
                        disabled={Boolean(busyAction)}
                      />
                      <span>
                        <strong>{agent.name}</strong>
                        <em>{agent.provider}</em>
                      </span>
                    </label>
                    <span className={`nexus-chip nexus-chip--${agent.status}`}>
                      {responseLabel(agent.status)}
                    </span>
                    <button
                      type="button"
                      onClick={() => void openAgent(agent.agent_id)}
                      disabled={Boolean(busyAction)}
                    >
                      {busyAction === `open:${agent.agent_id}` ? '開啟中...' : '開啟'}
                    </button>
                    {agent.last_error ? <p>{agent.last_error}</p> : null}
                  </article>
                ))}
              </div>
            )}
          </section>
        </aside>

        <section className="nexus-column nexus-column--main">
          <section className="nexus-surface nexus-holdings-surface">
            <div className="nexus-section-head">
              <span>持股資料</span>
              <strong>{investmentWatch.investmentHoldings.length}</strong>
            </div>
            <p className="nexus-scan-note">
              {workbookScanLabel}
              {selectedWorkbookSheet?.header_mode ? ` · ${selectedWorkbookSheet.header_mode}` : ''}
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
                  資料列
                  <strong>{workbookQuality.valid_data_row_count ?? '-'}</strong>
                </span>
                <span>
                  表頭
                  <strong>{workbookQuality.header_depth || 1} 列</strong>
                </span>
                <p>{workbookQuality.recommendation}</p>
              </div>
            ) : null}
            <div className="nexus-holding-table">
              <div className="nexus-holding-head">
                <span>代號</span>
                <span>名稱</span>
                <span>市場</span>
                <span>數量</span>
                <span>平均成本</span>
                <span>幣別</span>
              </div>
              {visibleHoldings.length === 0 ? (
                <p className="nexus-empty-line">尚無持股</p>
              ) : (
                visibleHoldings.map((holding, index) => (
                  <div
                    key={`${holding.symbol || 'holding'}:${index}`}
                    className="nexus-holding-row"
                  >
                    <strong>{holding.symbol || '-'}</strong>
                    <span>{holding.name || '-'}</span>
                    <span>{holding.market || '-'}</span>
                    <span>{formatInvestmentNumber(holding.quantity, 4)}</span>
                    <span>{formatInvestmentNumber(holding.average_cost, 4)}</span>
                    <span>{holding.currency || '-'}</span>
                  </div>
                ))
              )}
              {hiddenHoldingCount > 0 ? (
                <p className="nexus-empty-line">其餘 {hiddenHoldingCount} 檔已納入分析</p>
              ) : null}
            </div>
          </section>

          <section className="nexus-surface nexus-composer">
            <div className="nexus-section-head">
              <span>投資分析</span>
              <strong>{selectedAgentNames || '未選 AI'}</strong>
            </div>
            <textarea
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              placeholder="輸入要交給 AI 群組的投資問題"
              disabled={Boolean(busyAction)}
            />
            <div className="nexus-action-row">
              <button
                type="button"
                className="nexus-primary"
                onClick={() => void sendGroupMessage()}
                disabled={Boolean(busyAction)}
              >
                {busyAction === 'send' ? '收集中...' : '送給選取 AI'}
              </button>
              <button
                type="button"
                onClick={() =>
                  void investmentWatch.runInvestmentCommand(
                    'investment_watch_gemini_quote_search',
                    'Gemini 報價/搜尋'
                  )
                }
                disabled={Boolean(busyAction)}
              >
                Gemini 報價
              </button>
              <button
                type="button"
                onClick={() =>
                  void investmentWatch.runInvestmentCommand(
                    'investment_watch_gpt_extract_filter',
                    'GPT 特徵萃取'
                  )
                }
                disabled={Boolean(busyAction)}
              >
                GPT 萃取
              </button>
            </div>
          </section>

          <section className="nexus-thread" aria-label="群組訊息">
            {messages.length === 0 ? (
              <div className="nexus-empty">尚無訊息</div>
            ) : (
              messages.map((item) => (
                <article key={item.message_id} className="nexus-message">
                  <div className="nexus-message-head">
                    <strong>你</strong>
                    <span>{formatClock(item.created_at)}</span>
                  </div>
                  <p>{item.content}</p>
                  <div className="nexus-response-list">
                    {item.responses.map((response) => {
                      const agent = agentsById.get(response.agent_id)
                      return (
                        <section key={response.response_id} className="nexus-response">
                          <div>
                            <strong>{agent?.name || response.agent_id}</strong>
                            <span className={`nexus-chip nexus-chip--${response.status}`}>
                              {responseLabel(response.status)}
                            </span>
                          </div>
                          <pre>{response.content || response.error || '等待回覆'}</pre>
                        </section>
                      )
                    })}
                  </div>
                </article>
              ))
            )}
          </section>
        </section>

        <aside className="nexus-column nexus-column--right">
          <section className="nexus-surface nexus-local-ai-status">
            <div className="nexus-section-head">
              <span>本地AI狀態</span>
              <strong>{localAiStatus?.watch_status_label || '自動監測'}</strong>
            </div>
            <div className={`nexus-ai-score nexus-ai-score--${localAiStatus?.state || 'empty'}`}>
              <strong>{localAiScore}</strong>
              <span>{localAiStatusLabel}</span>
            </div>
            <p>{localAiRecommendation}</p>
            <div className="nexus-local-ai-meta">
              <span>{localAiStatus?.coverage_label || '等待持股資料'}</span>
              <span>風險 {localAiStatus?.warning_count ?? 0}</span>
              <span>重大 {localAiStatus?.critical_count ?? 0}</span>
            </div>
            {localAiActions.length > 0 ? (
              <ul className="nexus-local-ai-actions">
                {localAiActions.map((action) => (
                  <li key={action}>{action}</li>
                ))}
              </ul>
            ) : null}
            <div className="nexus-risk-board">
              <span>風險預告</span>
              {localAiWarnings.length === 0 ? (
                <p>尚無風險預告</p>
              ) : (
                localAiWarnings.map((warning, index) => (
                  <article
                    key={`${warning.code || 'risk'}:${warning.symbol || index}`}
                    className={`nexus-risk-row nexus-risk-row--${warning.severity || 'info'}`}
                  >
                    <strong>{warning.title || warning.code || '風險項目'}</strong>
                    <span>{warning.detail || warning.action || '-'}</span>
                  </article>
                ))
              )}
            </div>
            <div className="nexus-command-suggestions">
              <span>命令提示</span>
              <div>
                {localAiCommands.length === 0 ? (
                  <code>摘要 持股 離線</code>
                ) : (
                  localAiCommands.map((command) => <code key={command}>{command}</code>)
                )}
              </div>
            </div>
          </section>

          <section className="nexus-surface">
            <div className="nexus-section-head">
              <span>AI 分析紀錄</span>
              <strong>{investmentWatch.investmentRuns.length}</strong>
            </div>
            <form
              className="nexus-local-ai-command"
              onSubmit={(event) => {
                event.preventDefault()
                void investmentWatch.sendLocalRiskCommand()
              }}
            >
              <label htmlFor="local-risk-command">本地輔助AI命令</label>
              <input
                id="local-risk-command"
                value={investmentWatch.localRiskCommand}
                onChange={(event) => investmentWatch.setLocalRiskCommand(event.target.value)}
                placeholder={
                  investmentWatch.investmentHoldings.length === 0
                    ? '先上傳 Excel'
                    : '輸入本地命令'
                }
                disabled={Boolean(busyAction) || investmentWatch.investmentHoldings.length === 0}
              />
              <button type="submit" className="nexus-sr-only">
                送出本地輔助AI命令
              </button>
            </form>
            {localAiCommandPreview ? (
              <div className="nexus-command-result">
                <span>最近命令結果</span>
                <pre>{localAiCommandPreview}</pre>
              </div>
            ) : null}
            <div className="nexus-run-list">
              {latestInvestmentRuns.length === 0 ? (
                <p className="nexus-empty-line">尚無紀錄</p>
              ) : (
                latestInvestmentRuns.map((run) => {
                  const preview = (run.content || run.error || '').trim()
                  return (
                    <article key={run.run_id} className="nexus-run-row">
                      <strong>{investmentRunLabel(run)}</strong>
                      <span>
                        {investmentStatusLabel(run.status)} · {formatInvestmentClock(run.created_at)}
                      </span>
                      {preview ? <pre className="nexus-run-preview">{preview}</pre> : null}
                    </article>
                  )
                })
              )}
            </div>
          </section>

          <section className="nexus-surface">
            <div className="nexus-section-head">
              <span>共享記憶</span>
              <strong>{memoryItems.length}</strong>
            </div>
            <textarea
              className="nexus-memory-input"
              value={memoryDraft}
              onChange={(event) => setMemoryDraft(event.target.value)}
              placeholder="輸入要保留的規則或背景"
              disabled={Boolean(busyAction)}
            />
            <button type="button" onClick={() => void saveMemory()} disabled={Boolean(busyAction)}>
              儲存記憶
            </button>
            <div className="nexus-memory-list">
              {memoryItems.slice(0, 5).map((item) => (
                <article key={item.memory_id} className="nexus-memory-row">
                  <strong>{item.title}</strong>
                  <p>{item.content}</p>
                </article>
              ))}
            </div>
          </section>

          <section className="nexus-surface nexus-system">
            <div className="nexus-section-head">
              <span>系統</span>
              <strong>{socketLabel}</strong>
            </div>
            <span>資料庫：{paths.database || '載入中'}</span>
            <span>工作區：{paths.workspace || '載入中'}</span>
            <span>Edge：{paths.profile || '載入中'}</span>
            {safetyNotice ? <p>{safetyNotice}</p> : null}
          </section>
        </aside>
      </section>
    </main>
  )
}
