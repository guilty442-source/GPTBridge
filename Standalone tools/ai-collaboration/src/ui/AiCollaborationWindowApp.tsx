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
  session_state?: string
  login_state?: string
}

type ResponseItem = {
  agent_id?: string
  status?: string
  content?: string
  answer?: string
  response?: string
  text?: string
  error?: string
  error_code?: string
}

type GroupMessage = {
  message_id?: string
  content?: string
  responses?: ResponseItem[]
}

type CollabResult = {
  provider_id?: string
  response_id?: string
  response_text?: string
  response_status?: string
  capture_method?: string
  captured_at?: string
  adapter_version?: string
  error_code?: string
}

type CollabComparison = {
  common_points?: { text?: string; sources?: string[] }[]
  differences?: { text?: string; source_provider?: string }[]
  contradictions?: {
    topic?: string[]
    statements?: { provider_id?: string; text?: string }[]
  }[]
  unanswered_questions?: { question?: string; raised_by?: string }[]
}

type CollabTask = {
  task_id?: string
  request_id?: string
  mode?: string
  selected_providers?: string[]
  original_request?: string
  overall_status?: string
  fault_reference?: string
  provider_results?: CollabResult[]
  comparison?: CollabComparison
  synthesis?: { method?: string; summary?: string }
}

type CollaborationState = {
  ok?: boolean
  message?: string
  agents?: Agent[]
  messages?: GroupMessage[]
  message_id?: string
  group_message?: GroupMessage
  runtime_generation?: string
  collab_tasks?: CollabTask[]
  task?: CollabTask
}

const COLLAB_MODES = [
  { id: 'single', label: '單一 AI' },
  { id: 'compare', label: '多 AI 比較' },
  { id: 'sequential_review', label: '依序審查' },
] as const

type CollabMode = (typeof COLLAB_MODES)[number]['id']

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
  if (status === 'cancelled') return '已取消'
  if (status === 'opened') return '已開啟'
  if (status === 'waiting_user') return '等待使用者'
  if (status === 'aggregating') return '彙整中'
  if (status === 'partial') return '部分完成'
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
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [newAgentName, setNewAgentName] = useState('')
  const [newAgentProvider, setNewAgentProvider] = useState('')
  const [newAgentUrl, setNewAgentUrl] = useState('')
  const [draft, setDraft] = useState('')
  const [collabMode, setCollabMode] = useState<CollabMode>('single')
  const [collabTasks, setCollabTasks] = useState<CollabTask[]>([])
  const [activeTaskId, setActiveTaskId] = useState('')
  const [message, setMessage] = useState('AI協作工具已就緒')
  const [messageId, setMessageId] = useState('')
  const [responses, setResponses] = useState<ResponseItem[]>([])
  const [browserDrafts, setBrowserDrafts] = useState<Record<string, string>>({})
  const [busyAction, setBusyAction] = useState('')
  const loadedSelectionRef = useRef(false)
  const messageIdRef = useRef('')
  const inflightRequestRef = useRef('')
  const activeTaskIdRef = useRef('')

  const selectedAgentList = useMemo(
    () => agents.filter((agent) => selectedAgents.has(agent.agent_id)),
    [agents, selectedAgents]
  )
  const agentsById = useMemo(
    () => new Map(agents.map((agent) => [agent.agent_id, agent])),
    [agents]
  )
  const selectedAgentSummary = `${selectedAgentList.length} / ${agents.length}`
  const activeTask = useMemo(
    () =>
      collabTasks.find(
        (item) => String(item.task_id || '') === activeTaskId
      ) || null,
    [collabTasks, activeTaskId]
  )

  const request = useCallback(
    async (
      command: string,
      payload: Record<string, unknown> = {},
      timeoutMs = 30000
    ) => {
      const requestId =
        String(payload.request_id || '').trim() ||
        `${command}:${Date.now()}:${Math.random().toString(16).slice(2)}`
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
    if (Array.isArray(state.collab_tasks)) {
      setCollabTasks(state.collab_tasks)
    }
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

  const openProviderInBrowser = useCallback(
    (agent: Agent | undefined) => {
      if (!agent) return
      const targetUrl = agent.general_url || agent.home_url || ''
      if (!targetUrl) return
      setUrlInput(targetUrl)
      const bounds = browserBounds()
      void browserRef.current
        .openProvider(agent.agent_id, targetUrl, bounds ?? undefined)
        .then(() => syncBrowserBounds())
    },
    [browserBounds, syncBrowserBounds]
  )

  // Keep the view aligned while the window or panels resize.
  useEffect(() => {
    let mounted = true
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

    // Layout changes resize the browser stage without a window resize
    // event; keep the view aligned.
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
  }, [syncBrowserBounds])

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
      openProviderInBrowser(agent)
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
      openProviderInBrowser(agent)
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
      if (firstSelected) openProviderInBrowser(firstSelected)
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
    const selectedProviders = selectedAgentList.map((agent) => agent.provider)
    if (collabMode !== 'single' && selectedProviders.length < 2) {
      setMessage('此模式至少需要兩個 AI')
      return
    }
    const requestId = `ai_nexus_collab_start:${Date.now()}:${Math.random()
      .toString(16)
      .slice(2)}`
    inflightRequestRef.current = requestId
    setBusyAction('send')
    setMessage(`正在交給 ${selectedProviders.length} 個 AI 協作...`)
    try {
      const result = (await request(
        'ai_nexus_collab_start',
        {
          content: draft,
          provider_ids: selectedProviders,
          mode: collabMode,
          request_id: requestId,
          idempotency_key: requestId,
        },
        300000
      )) as CollaborationState
      if (result.ok === false) throw new Error(String(result.message || '送出失敗'))
      const task = result.task || {}
      const nextTaskId = String(task.task_id || '')
      if (nextTaskId) {
        activeTaskIdRef.current = nextTaskId
        setActiveTaskId(nextTaskId)
      }
      if (Array.isArray(result.collab_tasks)) setCollabTasks(result.collab_tasks)
      setDraft('')
      if (Array.isArray(result.agents)) setAgents(result.agents)
      setMessage(String(result.message || 'AI 協作已完成'))
    } catch (error) {
      setMessage(error instanceof Error ? error.message : 'AI 協作送出失敗')
    } finally {
      inflightRequestRef.current = ''
      setBusyAction('')
    }
  }

  const cancelSend = async () => {
    const requestId = inflightRequestRef.current
    const taskId = activeTaskIdRef.current
    setMessage('正在取消協作請求...')
    try {
      if (taskId) {
        await request('ai_nexus_collab_cancel', { task_id: taskId }, 15000)
      } else if (requestId) {
        sendCommand('toolbox_cancel_tool_run', { request_id: requestId })
      }
      await loadState(true)
    } catch {
      // best-effort: the runtime also cancels the in-flight task
    }
  }

  const submitBrowserResult = async (providerId: string) => {
    const taskId = activeTaskIdRef.current || activeTaskId
    if (!taskId) {
      setMessage('目前沒有等待中的協作任務')
      return
    }
    const draftKey = `${taskId}:${providerId}`
    const content = (browserDrafts[draftKey] || '').trim()
    if (!content) {
      setMessage('請先貼上瀏覽器中的 AI 回覆')
      return
    }
    setBusyAction(`browser:${providerId}`)
    try {
      const result = await request(
        'ai_nexus_collab_manual_result',
        { task_id: taskId, provider_id: providerId, content },
        60000
      )
      if (result.ok === false) {
        throw new Error(String(result.message || '送出瀏覽器回覆失敗'))
      }
      setBrowserDrafts((current) => ({ ...current, [draftKey]: '' }))
      setMessage(String(result.message || '已匯入手動回覆'))
      await loadState(true)
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '送出瀏覽器回覆失敗')
    } finally {
      setBusyAction('')
    }
  }

  return (
    <main className="ai-collab-app">
      {/* A544/A545: exactly one compact integrated card on top carrying the
          URL toolbar, the built-in AI list, selection, open/login, status,
          and the collaboration input.  No separate demand/list/diagnostic
          cards are allowed. */}
      <section className="ai-collab-topcard" role="group" aria-label="外部協作整合區">
        <div className="ai-collab-topcard-row ai-collab-urlrow">
          <button
            type="button"
            onClick={() => void browser.goBack()}
            disabled={!browser.state.canGoBack}
            aria-label="返回"
            title="返回"
          >
            ◀
          </button>
          <button
            type="button"
            onClick={() => void browser.goForward()}
            disabled={!browser.state.canGoForward}
            aria-label="前進"
            title="前進"
          >
            ▶
          </button>
          <button
            type="button"
            onClick={() => void browser.reload()}
            disabled={!browser.state.sessionId}
            aria-label="重新整理"
            title="重新整理"
          >
            ⟳
          </button>
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
              placeholder="輸入網址，例如 https://chatgpt.com"
            />
            <button type="submit" className="ai-collab-primary" disabled={!urlInput.trim()}>
              前往
            </button>
          </form>
          <span className="ai-collab-chip">{socketStatusLabel(socketStatus)}</span>
          {browser.state.loading ? (
            <span className="ai-collab-chip ai-collab-chip--running">載入中</span>
          ) : null}
        </div>
        {browser.state.error ? (
          <p className="ai-collab-error" role="alert">
            {browser.state.error}
          </p>
        ) : null}

        <div className="ai-collab-topcard-row ai-collab-agentrow" role="group" aria-label="內建 AI 名單">
          {agents.length === 0 ? (
            <p className="ai-collab-muted">AI 名單載入中，請確認後端連線...</p>
          ) : agents.map((agent) => (
            <span key={agent.agent_id} className="ai-collab-agent-chip">
              <label>
                <input
                  type="checkbox"
                  checked={selectedAgents.has(agent.agent_id)}
                  onChange={() => void toggleAgent(agent.agent_id)}
                  disabled={Boolean(busyAction)}
                />
                <strong>{agent.name}</strong>
              </label>
              <span className={`ai-collab-chip ai-collab-chip--${agent.status}`}>
                {responseLabel(agent.status)}
              </span>
              <button
                type="button"
                onClick={() => void openAgent(agent.agent_id)}
                disabled={Boolean(busyAction)}
              >
                {busyAction === `open:${agent.agent_id}` ? '開啟中' : '開啟'}
              </button>
              <button
                type="button"
                onClick={() => void authorizeAgent(agent.agent_id)}
                disabled={Boolean(busyAction)}
              >
                {busyAction === `authorize:${agent.agent_id}` ? '開啟中' : '登入'}
              </button>
            </span>
          ))}
          <span className="ai-collab-agentrow-actions">
            <button
              type="button"
              className="ai-collab-primary"
              onClick={() => void openSelectedAgents()}
              disabled={Boolean(busyAction) || selectedAgents.size === 0}
            >
              {busyAction === 'open-selected' ? '開啟中...' : '開啟選取'}
            </button>
            <button
              type="button"
              onClick={() => void loadState()}
              disabled={Boolean(busyAction)}
            >
              重新整理
            </button>
            <button
              type="button"
              onClick={() => void exportReport()}
              disabled={Boolean(busyAction)}
            >
              {busyAction === 'export-report' ? '匯出中...' : '匯出診斷'}
            </button>
            <button
              type="button"
              onClick={() => setSettingsOpen((value) => !value)}
              aria-expanded={settingsOpen}
            >
              {settingsOpen ? '關閉設定' : '設定'}
            </button>
            <span className="ai-collab-muted">{selectedAgentSummary} AI</span>
          </span>
        </div>
        {agents.some((agent) => agent.last_error) ? (
          <p className="ai-collab-error" role="alert">
            {agents.find((agent) => agent.last_error)?.last_error}
          </p>
        ) : null}

        {settingsOpen ? (
          <div className="ai-collab-settings" role="group" aria-label="設定">
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
          </div>
        ) : null}

        <div className="ai-collab-topcard-row ai-collab-composerrow" role="group" aria-label="協作需求輸入">
          <select
            className="ai-collab-mode-select"
            value={collabMode}
            onChange={(event) => setCollabMode(event.target.value as CollabMode)}
            disabled={Boolean(busyAction)}
            aria-label="協作模式"
          >
            {COLLAB_MODES.map((mode) => (
              <option key={mode.id} value={mode.id}>
                {mode.label}
              </option>
            ))}
          </select>
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
          <textarea
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            placeholder="輸入要交給已勾選 AI 的協作需求"
            disabled={busyAction === 'send'}
          />
          <button
            type="button"
            className="ai-collab-primary"
            onClick={() => void sendGroupMessage()}
            disabled={Boolean(busyAction) || !draft.trim() || selectedAgents.size === 0}
          >
            {busyAction === 'send' ? '協作中...' : '送出協作'}
          </button>
          {busyAction === 'send' ? (
            <button type="button" onClick={() => void cancelSend()}>
              取消
            </button>
          ) : null}
        </div>
        <p className="ai-collab-muted" role="status">
          {message}
        </p>
      </section>

      <section className="ai-collab-workspace" aria-label="外部協作雙欄工作區">
        <div className="ai-collab-left" role="region" aria-label="AI 協作結果">
          <div className="ai-collab-responses" role="group" aria-label="AI 回應">
            <div className="ai-collab-top-agents-head">
              <div>
                <span>AI 回應</span>
                <strong>
                  {activeTask
                    ? `${(activeTask.provider_results || []).length}`
                    : responses.length}
                </strong>
              </div>
              {collabTasks.length > 0 ? (
                <select
                  className="ai-collab-task-select"
                  value={activeTaskId}
                  onChange={(event) => {
                    activeTaskIdRef.current = event.target.value
                    setActiveTaskId(event.target.value)
                  }}
                  aria-label="選擇協作任務"
                >
                  {collabTasks.map((task) => (
                    <option
                      key={String(task.task_id)}
                      value={String(task.task_id)}
                    >
                      {responseLabel(String(task.overall_status || ''))} ·{' '}
                      {String(task.mode || '')} ·{' '}
                      {String(task.original_request || '').slice(0, 20)}
                    </option>
                  ))}
                </select>
              ) : null}
            </div>
            {activeTask ? (
              <>
                {(activeTask.provider_results || []).map((result) => {
                  const providerId = String(result.provider_id || '')
                  const status = String(result.response_status || '')
                  const draftKey = `${activeTask.task_id}:${providerId}`
                  const text = String(result.response_text || '')
                  return (
                    <article key={draftKey} className="ai-collab-response">
                      <div className="ai-collab-response-head">
                        <strong>{providerId}</strong>
                        <span className={`ai-collab-chip ai-collab-chip--${status}`}>
                          {responseLabel(status)}
                        </span>
                        {result.capture_method ? (
                          <span className="ai-collab-chip">
                            {result.capture_method === 'MANUAL' ? '手動匯入' : '自動擷取'}
                          </span>
                        ) : null}
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
                            onClick={() => void submitBrowserResult(providerId)}
                            disabled={
                              Boolean(busyAction) ||
                              !(browserDrafts[draftKey] || '').trim()
                            }
                          >
                            {busyAction === `browser:${providerId}` ? '送出中...' : '手動匯入'}
                          </button>
                        </div>
                      ) : null}
                      {result.error_code ? (
                        <p className="ai-collab-muted">{String(result.error_code)}</p>
                      ) : null}
                    </article>
                  )
                })}
                {activeTask.comparison &&
                (activeTask.comparison.common_points?.length ||
                  activeTask.comparison.differences?.length) ? (
                  <article className="ai-collab-response ai-collab-comparison">
                    <div className="ai-collab-response-head">
                      <strong>比較結果</strong>
                    </div>
                    {(activeTask.comparison.common_points || []).length > 0 ? (
                      <div>
                        <span className="ai-collab-muted">共同觀點</span>
                        {(activeTask.comparison.common_points || []).map(
                          (item, index) => (
                            <p key={`c${index}`} className="ai-collab-response-body">
                              · {item.text}
                            </p>
                          )
                        )}
                      </div>
                    ) : null}
                    {(activeTask.comparison.differences || []).length > 0 ? (
                      <div>
                        <span className="ai-collab-muted">各 AI 差異</span>
                        {(activeTask.comparison.differences || []).map(
                          (item, index) => (
                            <p key={`d${index}`} className="ai-collab-response-body">
                              · [{item.source_provider}] {item.text}
                            </p>
                          )
                        )}
                      </div>
                    ) : null}
                    {(activeTask.comparison.contradictions || []).length > 0 ? (
                      <div>
                        <span className="ai-collab-muted">相互矛盾</span>
                        {(activeTask.comparison.contradictions || []).map(
                          (item, index) => (
                            <p key={`k${index}`} className="ai-collab-response-body">
                              · {(item.statements || [])
                                .map((s) => `[${s.provider_id}] ${s.text}`)
                                .join(' / ')}
                            </p>
                          )
                        )}
                      </div>
                    ) : null}
                    {(activeTask.comparison.unanswered_questions || []).length > 0 ? (
                      <div>
                        <span className="ai-collab-muted">尚未回答</span>
                        {(activeTask.comparison.unanswered_questions || []).map(
                          (item, index) => (
                            <p key={`u${index}`} className="ai-collab-response-body">
                              · {item.question}
                            </p>
                          )
                        )}
                      </div>
                    ) : null}
                  </article>
                ) : null}
                {activeTask.synthesis?.summary ? (
                  <article className="ai-collab-response ai-collab-synthesis">
                    <div className="ai-collab-response-head">
                      <strong>整合結果</strong>
                      <span className="ai-collab-chip">
                        {activeTask.synthesis.method === 'governed-model'
                          ? '受管模型'
                          : '規則彙整'}
                      </span>
                    </div>
                    <p className="ai-collab-response-body ai-collab-synthesis-body">
                      {activeTask.synthesis.summary}
                    </p>
                  </article>
                ) : null}
              </>
            ) : responses.length === 0 ? (
              <p className="ai-collab-muted">送出協作後，AI 回應會顯示在這裡。</p>
            ) : (
              responses.map((response) => {
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
                    {response.error ? <p className="ai-collab-muted">{String(response.error)}</p> : null}
                  </article>
                )
              })
            )}
          </div>
        </div>
        <div className="ai-collab-right" ref={rightPanelRef} aria-label="內建瀏覽器網頁區">
          <div className="ai-collab-browser-canvas" />
        </div>
      </section>
    </main>
  )
}
