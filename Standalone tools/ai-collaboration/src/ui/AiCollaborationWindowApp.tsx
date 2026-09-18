import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useLocalBackendSocket } from './backendSocket'
import { useEmbeddedBrowser, type BrowserBounds } from './useEmbeddedBrowser'
import './ai-collaboration.css'

type Agent = {
  agent_id: string
  name: string
  provider: string
  home_url: string
  general_url: string
  investment_url: string
  star_training_url: string
  general_enabled: number
  investment_enabled: number
  business_capabilities: string[]
  enabled: number
  selected: number
  status: string
  last_error: string
}

type ResponseItem = {
  agent_id?: string
  status?: string
  content?: string
  answer?: string
  response?: string
  text?: string
  error?: string
}

type GroupMessage = {
  message_id?: string
  content?: string
  responses?: ResponseItem[]
}

type CollaborationState = {
  ok?: boolean
  message?: string
  agents?: Agent[]
  messages?: GroupMessage[]
  message_id?: string
  group_message?: GroupMessage
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

function responseLabel(status: string): string {
  if (status === 'completed') return '完成'
  if (status === 'running') return '執行中'
  if (status === 'waiting_verification') return '等待驗證'
  if (status === 'awaiting-user') return '等待瀏覽器操作'
  if (status === 'waiting') return '等待前序結果'
  if (status === 'failed') return '失敗'
  if (status === 'opened') return '已開啟'
  return '待命'
}

function responseText(response: ResponseItem): string {
  const value =
    response.content ?? response.answer ?? response.response ?? response.text ?? ''
  return String(value)
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
      // Handle the expected result event.
      if (detail.event === eventName) {
        if (requestId && String(payload.request_id || '') !== requestId) return
        window.clearTimeout(timer)
        window.removeEventListener('ipc_event', handler)
        resolve(payload as T)
        return
      }
      // Defensive: if the backend sends a generic "error" event that
      // carries a matching request_id, reject immediately instead of
      // waiting for the full timeout.
      if (detail.event === 'error' && requestId) {
        if (String(payload.request_id || '') === requestId) {
          window.clearTimeout(timer)
          window.removeEventListener('ipc_event', handler)
          reject(new Error(String(payload.message || 'PERMISSION_DENIED')))
        }
      }
    }
    timer = window.setTimeout(() => {
      window.removeEventListener('ipc_event', handler)
      reject(new Error(`等待 ${eventName} 逾時`))
    }, timeoutMs)
    window.addEventListener('ipc_event', handler)
  })
}

export function AiCollaborationWindowApp() {
  const { sendCommand, status: socketStatus, waitUntilConnected } = useLocalBackendSocket()
  const browser = useEmbeddedBrowser()
  const rightPanelRef = useRef<HTMLDivElement>(null)
  const [urlInput, setUrlInput] = useState('')
  const [agents, setAgents] = useState<Agent[]>([])
  const [selectedAgents, setSelectedAgents] = useState<Set<string>>(new Set())
  const [agentListCollapsed, setAgentListCollapsed] = useState(false)
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [newAgentName, setNewAgentName] = useState('')
  const [newAgentProvider, setNewAgentProvider] = useState('')
  const [newAgentUrl, setNewAgentUrl] = useState('')
  const [draft, setDraft] = useState('')
  const [message, setMessage] = useState('AI協作工具已就緒')
  const [messageId, setMessageId] = useState('')
  const [responses, setResponses] = useState<ResponseItem[]>([])
  const [browserDrafts, setBrowserDrafts] = useState<Record<string, string>>({})
  const [busyAction, setBusyAction] = useState('')
  const loadedSelectionRef = useRef(false)
  const messageIdRef = useRef('')

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

  const request = useCallback(
    async (
      command: string,
      payload: Record<string, unknown> = {},
      timeoutMs = 30000
    ) => {
      const requestId = `${command}:${Date.now()}:${Math.random().toString(16).slice(2)}`
      // Wait for the backend socket to finish connecting before sending.
      // This prevents the "後端連線尚未就緒" failure during the initial
      // loadState() call that fires before the WebSocket has opened.
      try {
        await waitUntilConnected(Math.min(timeoutMs, 15_000))
      } catch (error) {
        throw new Error(
          error instanceof Error ? error.message : '後端連線尚未就緒，指令未送出，請稍後再試。'
        )
      }
      const waitPromise = waitForIpcEvent<Record<string, unknown>>(
        `${command}_result`,
        timeoutMs,
        requestId
      )
      const sent = sendCommand(command, { ...payload, request_id: requestId })
      if (!sent.ok && !sent.queued) {
        const failure = (await waitPromise) as Record<string, unknown>
        throw new Error(
          String(failure.message || sent.message || '送出指令失敗')
        )
      }
      const response = await waitPromise
      return response
    },
    [sendCommand, waitUntilConnected]
  )

  const applyState = useCallback((state: CollaborationState) => {
    const nextAgents = Array.isArray(state.agents) ? state.agents : []
    setAgents(nextAgents)
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
        const trackedId = messageIdRef.current
        if (trackedId && Array.isArray(result.messages)) {
          const tracked = result.messages.find(
            (item) => String(item.message_id || '') === trackedId
          )
          if (tracked && Array.isArray(tracked.responses)) {
            setResponses(tracked.responses)
          }
        }
        setMessage((prev) => {
          if (!silent) return 'AI協作工具已載入'
          if (/失敗|尚未就緒|逾時|正在載入/.test(prev)) return 'AI協作工具已載入'
          return prev
        })
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
    const resynchronize = (event: Event) => {
      if ((event as CustomEvent).detail?.connected === true) void loadState(true)
    }
    window.addEventListener('socket_connected', resynchronize)
    return () => {
      window.clearInterval(timer)
      window.removeEventListener('socket_connected', resynchronize)
    }
  }, [loadState])

  const browserRef = useRef(browser)
  browserRef.current = browser

  const browserBounds = useCallback((): BrowserBounds | null => {
    const node = rightPanelRef.current
    if (!node) return null
    const rect = node.getBoundingClientRect()
    const width = Math.round(rect.width)
    const height = Math.round(rect.height)
    if (width < 1 || height < 1) return null
    return { x: Math.round(rect.x), y: Math.round(rect.y), width, height }
  }, [])

  // The panel owns the embedded browser: valid bounds show it, a collapsed
  // or hidden panel detaches it.  Nothing is ever displayed full-window.
  const syncBrowserBounds = useCallback(async () => {
    const bounds = browserBounds()
    if (!bounds) {
      void browserRef.current.hideBrowser()
      return
    }
    await browserRef.current.resize(bounds)
    void browserRef.current.showBrowser()
  }, [browserBounds])

  const handleNavigate = useCallback(
    (url: string) => {
      setUrlInput(url)
      const bounds = browserBounds()
      void browserRef.current.navigate(url, bounds ?? undefined).then(() =>
        syncBrowserBounds()
      )
    },
    [browserBounds, syncBrowserBounds]
  )

  useEffect(() => {
    let mounted = true
    const setup = async () => {
      const bounds = browserBounds()
      await browserRef.current.navigate(
        'https://www.google.com',
        bounds ?? undefined
      )
      if (!mounted) return
      await syncBrowserBounds()
      window.setTimeout(() => {
        if (mounted) void syncBrowserBounds()
      }, 120)
    }
    void setup()

    const handleResize = () => {
      window.setTimeout(() => {
        if (mounted) void syncBrowserBounds()
      }, 80)
    }
    const handleVisibility = () => {
      if (document.hidden) void browserRef.current.hideBrowser()
      else void syncBrowserBounds()
    }
    window.addEventListener('resize', handleResize)
    document.addEventListener('visibilitychange', handleVisibility)

    // Layout changes (collapsing the AI list, diagnostics, panels) resize the
    // browser stage without a window resize event; keep the view aligned.
    const stage = rightPanelRef.current
    const boundsObserver =
      typeof ResizeObserver !== 'undefined' && stage
        ? new ResizeObserver(() => {
            if (mounted) void syncBrowserBounds()
          })
        : null
    boundsObserver?.observe(stage as HTMLDivElement)

    return () => {
      mounted = false
      boundsObserver?.disconnect()
      window.removeEventListener('resize', handleResize)
      document.removeEventListener('visibilitychange', handleVisibility)
      void browserRef.current.hideBrowser()
    }
  }, [browserBounds, syncBrowserBounds])

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

  const selectSingleAgent = async (agentId: string) => {
    if (!agentId) return
    setSelectedAgents(new Set([agentId]))
    try {
      const result = (await request('ai_nexus_set_agent_selection', {
        agent_ids: [agentId],
      })) as CollaborationState
      if (Array.isArray(result.agents)) setAgents(result.agents)
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '儲存 AI 名單失敗')
    }
  }

  const openAgent = async (agentId: string) => {
    setBusyAction(`open:${agentId}`)
    try {
      const agent = agentsById.get(agentId)
      const result = await request(
        'ai_nexus_open_agent',
        { agent_id: agentId, business_scope: 'general' },
        30000
      )
      if (result.ok === false) throw new Error(String(result.message || '開啟失敗'))
      const targetUrl = agent?.general_url || agent?.home_url || ''
      if (targetUrl) handleNavigate(targetUrl)
      setMessage(`${agentId} 已在內建瀏覽器開啟`)
      await loadState(true)
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '開啟 AI 失敗')
    } finally {
      setBusyAction('')
    }
  }

  const authorizeAgent = async (agentId: string) => {
    setBusyAction(`authorize:${agentId}`)
    try {
      const agent = agentsById.get(agentId)
      const result = await request('ai_nexus_authorize_agent', { agent_id: agentId }, 30000)
      if (result.ok === false) throw new Error(String(result.message || '啟動授權失敗'))
      const targetUrl = agent?.general_url || agent?.home_url || ''
      if (targetUrl) handleNavigate(targetUrl)
      setMessage(`${agentId} 已在內建瀏覽器開啟；請完成一次登入或人機驗證`)
      await loadState(true)
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '啟動帳號授權失敗')
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
        {
          agent_ids: Array.from(selectedAgents),
          business_scope: 'general',
        },
        60000
      )) as CollaborationState
      if (result.ok === false) throw new Error(String(result.message || '開啟選取 AI 失敗'))
      applyState(result)
      const firstSelected = selectedAgentList[0]
      const targetUrl = firstSelected?.general_url || firstSelected?.home_url || ''
      if (targetUrl) handleNavigate(targetUrl)
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

  const updateAgentSetting = (
    agentId: string,
    field: 'general_url' | 'investment_url' | 'star_training_url' | 'general_enabled' | 'investment_enabled',
    value: string | number
  ) => {
    setAgents((current) =>
      current.map((agent) =>
        agent.agent_id === agentId ? { ...agent, [field]: value } : agent
      )
    )
  }

  const saveAgentBusinessSettings = async (agent: Agent) => {
    setBusyAction(`settings:${agent.agent_id}`)
    try {
      const result = (await request('ai_nexus_update_agent_business_settings', {
        agent_id: agent.agent_id,
        general_url: agent.general_url,
        investment_url: agent.investment_url,
        star_training_url: agent.star_training_url,
        general_enabled: Boolean(agent.general_enabled),
        investment_enabled: Boolean(agent.investment_enabled),
        business_capabilities: agent.business_capabilities,
      })) as CollaborationState
      if (result.ok === false) throw new Error(String(result.message || '設定儲存失敗'))
      if (Array.isArray(result.agents)) setAgents(result.agents)
      setMessage(String(result.message || '業務 URL 已儲存'))
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '業務 URL 儲存失敗')
    } finally {
      setBusyAction('')
    }
  }

  const addAgent = async () => {
    if (!newAgentName.trim()) {
      setMessage('請輸入 AI 名稱')
      return
    }
    if (!newAgentUrl.trim()) {
      setMessage('請輸入 AI 網址')
      return
    }
    setBusyAction('add-agent')
    try {
      const result = (await request('ai_nexus_add_agent', {
        name: newAgentName.trim(),
        provider: newAgentProvider.trim(),
        home_url: newAgentUrl.trim(),
      })) as CollaborationState
      if (result.ok === false) throw new Error(String(result.message || '新增 AI 失敗'))
      if (Array.isArray(result.agents)) setAgents(result.agents)
      setNewAgentName('')
      setNewAgentProvider('')
      setNewAgentUrl('')
      setMessage(String(result.message || '已新增 AI'))
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '新增 AI 失敗')
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
          business_scope: 'general',
          business_task: 'general',
        },
        180000
      )) as CollaborationState
      if (result.ok === false) throw new Error(String(result.message || '送出失敗'))
      const groupMessage = result.group_message || {}
      const nextMessageId = String(groupMessage.message_id || result.message_id || '')
      if (nextMessageId) {
        messageIdRef.current = nextMessageId
        setMessageId(nextMessageId)
      }
      if (Array.isArray(groupMessage.responses)) setResponses(groupMessage.responses)
      setDraft('')
      if (Array.isArray(result.agents)) setAgents(result.agents)
      setMessage(String(result.message || 'AI 協作已完成'))
    } catch (error) {
      setMessage(error instanceof Error ? error.message : 'AI 協作送出失敗')
    } finally {
      setBusyAction('')
    }
  }

  const submitBrowserResult = async (agentId: string) => {
    const activeMessageId = messageIdRef.current || messageId
    if (!activeMessageId) {
      setMessage('目前沒有等待中的瀏覽器任務')
      return
    }
    const draftKey = `${activeMessageId}:${agentId}`
    const content = (browserDrafts[draftKey] || '').trim()
    if (!content) {
      setMessage('請先貼上瀏覽器中的 AI 回覆')
      return
    }
    setBusyAction(`browser:${agentId}`)
    try {
      const result = await request(
        'ai_nexus_complete_browser_response',
        { message_id: activeMessageId, agent_id: agentId, content },
        60000
      )
      if (result.ok === false) {
        throw new Error(String(result.message || '送出瀏覽器回覆失敗'))
      }
      setBrowserDrafts((current) => ({ ...current, [draftKey]: '' }))
      setMessage(String(result.message || '已送出瀏覽器回覆'))
      await loadState(true)
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '送出瀏覽器回覆失敗')
    } finally {
      setBusyAction('')
    }
  }

  return (
    <main className="ai-collab-app">
      <section className="ai-collab-workspace" aria-label="外部協作雙欄工作區">
        <div className="ai-collab-left" aria-label="協作輸入與 AI 清單">
          <section className="ai-collab-integrated" aria-label="外部協作控制">
            <div className="ai-collab-top-agents-head">
              <div>
                <span>外部協作</span>
                <strong>{socketStatusLabel(socketStatus)}</strong>
              </div>
              <span className="ai-collab-muted">
                {selectedAgentSummary} AI｜協作記憶自動記錄
              </span>
            </div>
            <form
              className="ai-collab-url-form"
              onSubmit={(e) => {
                e.preventDefault()
                handleNavigate(urlInput)
              }}
            >
              <input
                type="text"
                className="ai-collab-url-input"
                value={urlInput}
                onChange={(event) => setUrlInput(event.target.value)}
                placeholder="輸入網址，例如 google.com 或 https://chat.openai.com"
                disabled={browser.state.loading}
              />
              <button
                type="submit"
                className="ai-collab-primary"
                disabled={browser.state.loading || !urlInput.trim()}
              >
                {browser.state.loading ? '載入中...' : '前往'}
              </button>
            </form>
            <div className="ai-collab-toolbar">
              <button type="button" onClick={() => void handleNavigate('https://www.google.com')}>
                Google
              </button>
              <button
                type="button"
                onClick={() => void openSelectedAgents()}
                disabled={Boolean(busyAction) || selectedAgents.size === 0}
              >
                {busyAction === 'open-selected' ? '開啟中...' : '在內建瀏覽器開啟'}
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
              <button
                type="button"
                onClick={() => setSettingsOpen((value) => !value)}
                aria-expanded={settingsOpen}
              >
                {settingsOpen ? '關閉設定' : '設定'}
              </button>
            </div>
            <p className="ai-collab-muted" role="status">
              {message}
            </p>
          </section>
          {settingsOpen ? (
            <section className="ai-collab-settings" aria-label="設定">
              <div className="ai-collab-settings-block">
                <span>新增 AI 名單</span>
                <input
                  type="text"
                  value={newAgentName}
                  onChange={(event) => setNewAgentName(event.target.value)}
                  placeholder="AI 名稱，例如 Copilot"
                  disabled={Boolean(busyAction)}
                />
                <input
                  type="text"
                  value={newAgentProvider}
                  onChange={(event) => setNewAgentProvider(event.target.value)}
                  placeholder="提供者（可留空）"
                  disabled={Boolean(busyAction)}
                />
                <input
                  type="url"
                  value={newAgentUrl}
                  onChange={(event) => setNewAgentUrl(event.target.value)}
                  placeholder="https://..."
                  disabled={Boolean(busyAction)}
                />
                <button
                  type="button"
                  className="ai-collab-primary"
                  onClick={() => void addAgent()}
                  disabled={Boolean(busyAction) || !newAgentName.trim() || !newAgentUrl.trim()}
                >
                  {busyAction === 'add-agent' ? '新增中...' : '新增 AI'}
                </button>
              </div>
              <div className="ai-collab-settings-block">
                <span>各 AI 網址設定</span>
                {agents.map((agent) => (
                  <details key={agent.agent_id} className="ai-collab-agent-settings">
                    <summary>{agent.name}</summary>
                    <label>
                      <span>一般業務 URL</span>
                      <input
                        type="url"
                        value={agent.general_url || ''}
                        onChange={(event) => updateAgentSetting(agent.agent_id, 'general_url', event.target.value)}
                        disabled={Boolean(busyAction)}
                      />
                    </label>
                    <div className="ai-collab-agent-business-flags">
                      <label>
                        <input
                          type="checkbox"
                          checked={Boolean(agent.general_enabled)}
                          onChange={(event) => updateAgentSetting(agent.agent_id, 'general_enabled', event.target.checked ? 1 : 0)}
                        />
                        一般
                      </label>
                      <button type="button" onClick={() => void saveAgentBusinessSettings(agent)} disabled={Boolean(busyAction)}>
                        {busyAction === `settings:${agent.agent_id}` ? '儲存中...' : '儲存 URL'}
                      </button>
                    </div>
                  </details>
                ))}
              </div>
            </section>
          ) : null}
          <section className="ai-collab-top-agents" aria-label="內建 AI 清單">
            <div className="ai-collab-top-agents-head">
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
              <div className="ai-collab-agent-list ai-collab-agent-list--top">
                {agents.length === 0 ? (
                  <p className="ai-collab-muted">AI 名單載入中，請確認後端連線...</p>
                ) : agents.map((agent) => (
                  <article key={agent.agent_id} className="ai-collab-agent ai-collab-agent--top">
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
                    <div className="ai-collab-agent-actions">
                      <button type="button" onClick={() => void openAgent(agent.agent_id)} disabled={Boolean(busyAction)}>
                        {busyAction === `open:${agent.agent_id}` ? '開啟中...' : '開啟'}
                      </button>
                      <button type="button" onClick={() => void authorizeAgent(agent.agent_id)} disabled={Boolean(busyAction)}>
                        {busyAction === `authorize:${agent.agent_id}` ? '開啟中...' : '瀏覽器登入'}
                      </button>
                    </div>
                    {agent.last_error ? <p>{agent.last_error}</p> : null}
                  </article>
                ))}
              </div>
            )}
          </section>
          <section className="ai-collab-top-composer" aria-label="協作需求輸入">
            <div className="ai-collab-preset-row">
              <select
                className="ai-collab-agent-select"
                value={selectedAgents.size === 1 ? Array.from(selectedAgents)[0] : ''}
                onChange={(event) => void selectSingleAgent(event.target.value)}
                disabled={Boolean(busyAction) || agents.length === 0}
                aria-label="選擇協作 AI"
              >
                <option value="">
                  {selectedAgents.size === 0
                    ? '選擇 AI…'
                    : `已選 ${selectedAgents.size} 個 AI`}
                </option>
                {agents.map((agent) => (
                  <option key={agent.agent_id} value={agent.agent_id}>
                    {agent.name}
                  </option>
                ))}
              </select>
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
              placeholder="輸入要交給已勾選 AI 的協作需求"
              disabled={Boolean(busyAction)}
            />
            <button
              type="button"
              className="ai-collab-primary"
              onClick={() => void sendGroupMessage()}
              disabled={Boolean(busyAction) || !draft.trim() || selectedAgents.size === 0}
            >
              {busyAction === 'send' ? '協作中...' : '送出協作'}
            </button>
          </section>
          <section className="ai-collab-responses" aria-label="AI 回應">
            <div className="ai-collab-top-agents-head">
              <div>
                <span>AI 回應</span>
                <strong>{responses.length}</strong>
              </div>
            </div>
            {responses.length === 0 ? (
              <p className="ai-collab-muted">送出協作後，AI 回應會顯示在這裡。</p>
            ) : responses.map((response) => {
              const agentId = String(response.agent_id || '')
              const status = String(response.status || '')
              const draftKey = `${messageId}:${agentId}`
              const text = responseText(response)
              return (
                <article key={draftKey} className="ai-collab-response">
                  <div className="ai-collab-response-head">
                    <strong>{agentsById.get(agentId)?.name || agentId || 'AI'}</strong>
                    <span className={`ai-collab-chip ai-collab-chip--${status}`}>
                      {responseLabel(status)}
                    </span>
                  </div>
                  {text ? <p className="ai-collab-response-body">{text}</p> : null}
                  {status === 'awaiting-user' ? (
                    <div className="ai-collab-browser-submit">
                      <textarea
                        value={browserDrafts[draftKey] || ''}
                        onChange={(event) =>
                          setBrowserDrafts((current) => ({
                            ...current,
                            [draftKey]: event.target.value,
                          }))
                        }
                        placeholder="完成瀏覽器操作後，將 AI 回覆貼回這裡再送出。"
                        disabled={Boolean(busyAction)}
                      />
                      <button
                        type="button"
                        className="ai-collab-primary"
                        onClick={() => void submitBrowserResult(agentId)}
                        disabled={Boolean(busyAction) || !(browserDrafts[draftKey] || '').trim()}
                      >
                        {busyAction === `browser:${agentId}` ? '送出中...' : '送出訊息'}
                      </button>
                    </div>
                  ) : null}
                  {response.error ? <p className="ai-collab-muted">{String(response.error)}</p> : null}
                </article>
              )
            })}
          </section>
        </div>
        <div className="ai-collab-right" ref={rightPanelRef} aria-label="內建瀏覽器網頁區">
          <div className="ai-collab-browser-canvas" />
        </div>
      </section>
    </main>
  )
}
