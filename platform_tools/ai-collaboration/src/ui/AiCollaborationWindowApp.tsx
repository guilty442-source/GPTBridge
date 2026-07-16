import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useLocalBackendSocket } from './backendSocket'
import './ai-collaboration.css'

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

type CollaborationDiagnostics = {
  state?: string
  message?: string
  agents?: {
    total?: number
    selected?: number
    enabled?: number
    status_counts?: Record<string, number>
  }
  collaboration?: {
    message_count?: number
    memory_count?: number
    task_count?: number
    completed_responses?: number
    failed_responses?: number
    waiting_verification?: number
    latest_message_id?: string
  }
  browser?: {
    initialized?: boolean
    profile_path?: string
    page_count?: number
  }
  generated_at?: string
}

type CollaborationState = {
  ok?: boolean
  message?: string
  agents?: Agent[]
  messages?: GroupMessage[]
  memory_items?: MemoryItem[]
  diagnostics?: CollaborationDiagnostics
  database_path?: string
  workspace_path?: string
  browser_profile_path?: string
  safety_notice?: string
}

const PROMPT_PRESETS = [
  {
    id: 'summarize',
    label: '摘要',
    prompt: '請整理重點、共識、分歧與需要補資料的地方，最後列出 3 個下一步。',
  },
  {
    id: 'compare',
    label: '比較',
    prompt: '請用表格比較各方案的優點、缺點、風險、成本、適用情境，最後給出建議排序。',
  },
  {
    id: 'challenge',
    label: '反證',
    prompt: '請刻意找出這個想法可能錯在哪裡，列出反例、盲點、失敗條件與驗證方式。',
  },
  {
    id: 'plan',
    label: '行動',
    prompt: '請把這件事拆成可執行步驟，標出優先順序、依賴、風險與完成定義。',
  },
]

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
  if (status === 'opened') return '已開啟'
  return '待命'
}

function socketStatusLabel(status: string): string {
  if (status === 'Connected') return '已連線'
  if (status === 'Connecting') return '連線中'
  if (status === 'Disconnected') return '未連線'
  if (status === 'Error') return '連線錯誤'
  return status
}

function diagnosticsLabel(state?: string): string {
  if (state === 'ready') return '可用'
  if (state === 'running') return '協作中'
  if (state === 'attention') return '需檢查'
  if (state === 'setup') return '待設定'
  if (state === 'empty') return '無可用 AI'
  return '待命'
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

export function AiCollaborationWindowApp() {
  const { sendCommand, status: socketStatus } = useLocalBackendSocket()
  const [agents, setAgents] = useState<Agent[]>([])
  const [messages, setMessages] = useState<GroupMessage[]>([])
  const [memoryItems, setMemoryItems] = useState<MemoryItem[]>([])
  const [selectedAgents, setSelectedAgents] = useState<Set<string>>(new Set())
  const [agentListCollapsed, setAgentListCollapsed] = useState(true)
  const [draft, setDraft] = useState('')
  const [memoryDraft, setMemoryDraft] = useState('')
  const [message, setMessage] = useState('AI協作工具已就緒')
  const [busyAction, setBusyAction] = useState('')
  const [paths, setPaths] = useState({ workspace: '', database: '', profile: '' })
  const [safetyNotice, setSafetyNotice] = useState('')
  const [diagnostics, setDiagnostics] = useState<CollaborationDiagnostics | null>(null)
  const loadedSelectionRef = useRef(false)

  const selectedAgentList = useMemo(
    () => agents.filter((agent) => selectedAgents.has(agent.agent_id)),
    [agents, selectedAgents]
  )
  const agentsById = useMemo(
    () => new Map(agents.map((agent) => [agent.agent_id, agent])),
    [agents]
  )
  const selectedAgentSummary = `${selectedAgentList.length} / ${agents.length}`
  const selectedAgentNames = selectedAgentList.map((agent) => agent.name).join('、')
  const failedResponses = diagnostics?.collaboration?.failed_responses || 0
  const waitingVerification = diagnostics?.collaboration?.waiting_verification || 0
  const openedPages = diagnostics?.browser?.page_count || 0

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
        throw new Error(sent.message || '送出指令失敗')
      }
      const response = await waitPromise
      const maybeMemoryItems = (response as CollaborationState).memory_items
      if (Array.isArray(maybeMemoryItems)) {
        setMemoryItems(maybeMemoryItems)
      }
      return response
    },
    [sendCommand]
  )

  const applyState = useCallback((state: CollaborationState) => {
    const nextAgents = Array.isArray(state.agents) ? state.agents : []
    setAgents(nextAgents)
    setMessages(Array.isArray(state.messages) ? state.messages : [])
    setMemoryItems(Array.isArray(state.memory_items) ? state.memory_items : [])
    setDiagnostics(state.diagnostics && typeof state.diagnostics === 'object' ? state.diagnostics : null)
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
        const result = (await request('ai_nexus_get_state', {}, 15000)) as CollaborationState
        if (result.ok === false) throw new Error(String(result.message || '載入失敗'))
        applyState(result)
        if (!silent) setMessage('AI協作工具已載入')
      } catch (error) {
        if (!silent) {
          setMessage(error instanceof Error ? error.message : '載入 AI協作工具失敗')
        }
      }
    },
    [applyState, request]
  )

  useEffect(() => {
    void loadState()
    const timer = window.setInterval(() => {
      void loadState(true)
    }, 5000)
    return () => window.clearInterval(timer)
  }, [loadState])

  const toggleAgent = async (agentId: string) => {
    const next = new Set(selectedAgents)
    if (next.has(agentId)) next.delete(agentId)
    else next.add(agentId)
    setSelectedAgents(next)
    try {
      const result = (await request('ai_nexus_set_agent_selection', {
        agent_ids: Array.from(next),
      })) as CollaborationState
      if (Array.isArray(result.agents)) setAgents(result.agents)
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '儲存 AI 名單失敗')
    }
  }

  const openAgent = async (agentId: string) => {
    setBusyAction(`open:${agentId}`)
    try {
      const result = await request('ai_nexus_open_agent', { agent_id: agentId }, 30000)
      if (result.ok === false) throw new Error(String(result.message || '開啟失敗'))
      setMessage(`${agentId} 已在獨立 Edge 工作階段開啟`)
      await loadState(true)
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '開啟 AI 失敗')
    } finally {
      setBusyAction('')
    }
  }

  const openSelectedAgents = async () => {
    if (selectedAgents.size === 0) {
      setMessage('請至少選擇一個 AI')
      return
    }
    setBusyAction('open-selected')
    try {
      const result = (await request(
        'ai_nexus_open_selected_agents',
        { agent_ids: Array.from(selectedAgents) },
        60000
      )) as CollaborationState
      if (result.ok === false) throw new Error(String(result.message || '開啟選取 AI 失敗'))
      applyState(result)
      setMessage(String(result.message || '已開啟選取 AI'))
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '開啟選取 AI 失敗')
    } finally {
      setBusyAction('')
    }
  }

  const exportReport = async () => {
    setBusyAction('export-report')
    try {
      const result = await request('ai_nexus_export_report', {}, 10000)
      if (result.ok === false) throw new Error(String(result.message || '診斷報告匯出失敗'))
      const reportPath = String(result.report_path || '')
      setMessage(reportPath ? `診斷報告已匯出：${reportPath}` : '診斷報告已匯出')
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '診斷報告匯出失敗')
    } finally {
      setBusyAction('')
    }
  }

  const applyPromptPreset = (prompt: string) => {
    setDraft((current) => {
      const trimmed = current.trim()
      return trimmed ? `${trimmed}\n\n${prompt}` : prompt
    })
  }

  const sendGroupMessage = async () => {
    if (!draft.trim()) {
      setMessage('請輸入要交給 AI 協作的內容')
      return
    }
    if (selectedAgents.size === 0) {
      setMessage('請至少選擇一個 AI')
      return
    }
    setBusyAction('send')
    setMessage(`正在交給 ${selectedAgents.size} 個 AI 協作...`)
    try {
      const result = (await request(
        'ai_nexus_send_message',
        {
          content: draft,
          agent_ids: Array.from(selectedAgents),
        },
        180000
      )) as CollaborationState
      if (result.ok === false) throw new Error(String(result.message || '送出失敗'))
      setDraft('')
      if (Array.isArray(result.messages)) setMessages(result.messages)
      if (Array.isArray(result.agents)) setAgents(result.agents)
      setMessage(String(result.message || 'AI 協作已完成'))
    } catch (error) {
      setMessage(error instanceof Error ? error.message : 'AI 協作送出失敗')
    } finally {
      setBusyAction('')
    }
  }

  const saveMemory = async () => {
    if (!memoryDraft.trim()) {
      setMessage('請輸入共享記憶內容')
      return
    }
    setBusyAction('memory')
    try {
      const result = (await request('ai_nexus_add_memory', {
        kind: 'note',
        content: memoryDraft,
      })) as CollaborationState
      if (result.ok === false) throw new Error(String(result.message || '儲存失敗'))
      setMemoryDraft('')
      if (Array.isArray(result.memory_items)) setMemoryItems(result.memory_items)
      setMessage('共享記憶已儲存')
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '儲存共享記憶失敗')
    } finally {
      setBusyAction('')
    }
  }

  return (
    <main className="ai-collab-app">
      <header className="ai-collab-topbar">
        <div>
          <p>獨立應用程式</p>
          <h1>AI協作工具</h1>
          <div>
            <span>{socketStatusLabel(socketStatus)}</span>
            <span>{selectedAgentSummary} AI</span>
            <span>{memoryItems.length} 筆記憶</span>
          </div>
        </div>
        <div className="ai-collab-toolbar">
          <button
            type="button"
            onClick={() => void openSelectedAgents()}
            disabled={Boolean(busyAction) || selectedAgents.size === 0}
          >
            {busyAction === 'open-selected' ? '開啟中...' : '開啟選取 AI'}
          </button>
          <button
            type="button"
            onClick={() => void exportReport()}
            disabled={Boolean(busyAction)}
          >
            {busyAction === 'export-report' ? '匯出中...' : '匯出診斷'}
          </button>
          <button type="button" onClick={() => void loadState()} disabled={Boolean(busyAction)}>
            重新整理
          </button>
        </div>
      </header>

      <section className="ai-collab-status">
        <span>{message}</span>
        <span>{selectedAgentNames || '尚未選擇 AI'}</span>
      </section>

      <section className={`ai-collab-diagnostics ai-collab-diagnostics--${diagnostics?.state || 'idle'}`}>
        <div>
          <span>整體狀態</span>
          <strong>{diagnosticsLabel(diagnostics?.state)}</strong>
          <p>{diagnostics?.message || '等待診斷資料'}</p>
        </div>
        <div>
          <span>選取 AI</span>
          <strong>{diagnostics?.agents?.selected ?? selectedAgents.size}</strong>
          <p>可用 {diagnostics?.agents?.enabled ?? agents.length} / 全部 {diagnostics?.agents?.total ?? agents.length}</p>
        </div>
        <div>
          <span>回覆狀態</span>
          <strong>{failedResponses + waitingVerification}</strong>
          <p>失敗 {failedResponses} · 待驗證 {waitingVerification}</p>
        </div>
        <div>
          <span>瀏覽器</span>
          <strong>{diagnostics?.browser?.initialized ? '已啟動' : '待啟動'}</strong>
          <p>頁面 {openedPages}</p>
        </div>
      </section>

      <section className="ai-collab-grid">
        <aside className="ai-collab-panel">
          <div className="ai-collab-section-head">
            <div>
              <span>AI 名單</span>
              <strong>{selectedAgentSummary}</strong>
            </div>
            <button
              type="button"
              onClick={() => setAgentListCollapsed((value) => !value)}
              aria-expanded={!agentListCollapsed}
            >
              {agentListCollapsed ? '展開' : '折疊'}
            </button>
          </div>
          {agentListCollapsed ? (
            <p className="ai-collab-muted">{selectedAgentNames || '尚未選擇 AI'}</p>
          ) : (
            <div className="ai-collab-agent-list">
              {agents.map((agent) => (
                <article key={agent.agent_id} className="ai-collab-agent">
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
                  <span className={`ai-collab-chip ai-collab-chip--${agent.status}`}>
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
        </aside>

        <section className="ai-collab-main">
          <section className="ai-collab-panel ai-collab-composer">
            <div className="ai-collab-section-head">
              <div>
                <span>協作命令</span>
                <strong>{selectedAgentNames || '尚未選擇 AI'}</strong>
              </div>
            </div>
            <div className="ai-collab-preset-row">
              {PROMPT_PRESETS.map((preset) => (
                <button
                  key={preset.id}
                  type="button"
                  onClick={() => applyPromptPreset(preset.prompt)}
                  disabled={Boolean(busyAction)}
                >
                  {preset.label}
                </button>
              ))}
            </div>
            <textarea
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              placeholder="輸入要交給外部 AI 協作的需求"
              disabled={Boolean(busyAction)}
            />
            <button
              type="button"
              className="ai-collab-primary"
              onClick={() => void sendGroupMessage()}
              disabled={Boolean(busyAction)}
            >
              {busyAction === 'send' ? '協作中...' : '送出協作'}
            </button>
          </section>

          <section className="ai-collab-thread">
            {messages.length === 0 ? (
              <div className="ai-collab-empty">尚無協作紀錄</div>
            ) : (
              messages.map((item) => (
                <article key={item.message_id} className="ai-collab-message">
                  <div className="ai-collab-message-head">
                    <strong>需求</strong>
                    <span>{formatClock(item.created_at)}</span>
                  </div>
                  <p>{item.content}</p>
                  <div className="ai-collab-response-list">
                    {item.responses.map((response) => {
                      const agent = agentsById.get(response.agent_id)
                      return (
                        <section key={response.response_id} className="ai-collab-response">
                          <div>
                            <strong>{agent?.name || response.agent_id}</strong>
                            <span className={`ai-collab-chip ai-collab-chip--${response.status}`}>
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

        <aside className="ai-collab-panel">
          <div className="ai-collab-section-head">
            <div>
              <span>共享記憶</span>
              <strong>{memoryItems.length}</strong>
            </div>
          </div>
          <textarea
            className="ai-collab-memory-input"
            value={memoryDraft}
            onChange={(event) => setMemoryDraft(event.target.value)}
            placeholder="輸入要保存給 AI 協作工具的共享記憶"
            disabled={Boolean(busyAction)}
          />
          <button type="button" onClick={() => void saveMemory()} disabled={Boolean(busyAction)}>
            儲存記憶
          </button>
          <div className="ai-collab-memory-list">
            {memoryItems.slice(0, 8).map((item) => (
              <article key={item.memory_id} className="ai-collab-memory">
                <strong>{item.title}</strong>
                <p>{item.content}</p>
              </article>
            ))}
          </div>
          <section className="ai-collab-system">
            <span>資料庫：{paths.database || '尚未載入'}</span>
            <span>工作區：{paths.workspace || '尚未載入'}</span>
            <span>Edge：{paths.profile || '尚未載入'}</span>
            {safetyNotice ? <p>{safetyNotice}</p> : null}
          </section>
        </aside>
      </section>
    </main>
  )
}
