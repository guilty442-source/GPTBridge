import Editor from '@monaco-editor/react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useLocalBackendSocket } from './backendSocket'
import './agent-coder.css'

interface ApplicationOption {
  id: string
  name: string
  status: string
}

interface RescueDiagnostic {
  ok?: boolean
  path?: string
  language?: string
  size_bytes?: number
  character_count?: number
  line_count?: number
  risk_level?: string
  warnings?: string[]
  test_targets?: string[]
  test_command?: string
  recommendations?: string[]
  protected?: boolean
  message?: string
}

type Operation =
  | 'idle'
  | 'loading'
  | 'creating'
  | 'opening'
  | 'saving'
  | 'reviewing'
  | 'intervening'
  | 'testing'

const DEFAULT_INSTRUCTION =
  '請檢查並優化此應用程式程式碼，修正錯誤並確保可在 Windows 11 穩定執行。若需要，請同時調整相關測試與專案設定。'

const QUICK_INSTRUCTIONS = [
  {
    label: '錯誤修復',
    value:
      '請優先找出目前檔案會造成執行失敗、IPC 失敗、型別錯誤或測試失敗的原因，做最小必要修補並保留既有架構。',
  },
  {
    label: '效能優化',
    value:
      '請檢查是否有重複計算、過度渲染、低效率 I/O 或可快取的流程，改成更高效但容易維護的寫法。',
  },
  {
    label: '架構整理',
    value:
      '請依照現有專案分層規則整理此檔案責任邊界，降低耦合，不新增不必要抽象，並同步相關測試。',
  },
  {
    label: '前端體驗',
    value:
      '請檢查此畫面的狀態、錯誤提示、按鈕可用性與版面密度，修正讓操作流程更清楚且不破壞現有設計。',
  },
]

function formatClock(timestamp: number): string {
  return new Date(timestamp).toLocaleTimeString('zh-TW', { hour12: false })
}

function formatBytes(value?: number): string {
  if (!Number.isFinite(value || 0) || !value) return '0 B'
  if (value < 1024) return `${value} B`
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`
  return `${(value / 1024 / 1024).toFixed(2)} MB`
}

function editorLanguage(path: string): string {
  const extension = path.split('.').pop()?.toLowerCase()
  if (extension === 'tsx' || extension === 'ts') return 'typescript'
  if (extension === 'py') return 'python'
  if (extension === 'json') return 'json'
  if (extension === 'css') return 'css'
  if (extension === 'html') return 'html'
  return 'javascript'
}

function riskLabel(level?: string): string {
  if (level === 'blocked') return '受保護'
  if (level === 'warning') return '高風險'
  if (level === 'attention') return '需注意'
  if (level === 'ready') return '可救援'
  return '未診斷'
}

function formatTestSummary(result: Record<string, unknown>): string {
  const targets = Array.isArray(result.targets)
    ? result.targets.map((item) => String(item)).filter(Boolean)
    : []
  const command = String(result.command || '')
  const duration =
    typeof result.duration_ms === 'number'
      ? `${(result.duration_ms / 1000).toFixed(2)}s`
      : ''
  return [
    targets.length ? `測試目標：${targets.join(', ')}` : '',
    command ? `命令：${command}` : '',
    duration ? `耗時：${duration}` : '',
  ]
    .filter(Boolean)
    .join('\n')
}

function normalizeApplications(payload: unknown): ApplicationOption[] {
  const baseList: ApplicationOption[] = [
    {
      id: 'main-system',
      name: '主系統 (GPTBridge)',
      status: 'running',
    },
  ]

  if (!Array.isArray(payload)) return baseList

  const tools = payload
    .map((item) => {
      const entry = item as Record<string, unknown>
      const id = String(entry.id ?? '').trim()
      if (!id || id === 'agent-coder') return null
      return {
        id,
        name: String(entry.name ?? id).trim() || id,
        status: String(entry.status ?? 'stopped'),
      }
    })
    .filter((entry): entry is ApplicationOption => entry !== null)

  return [...baseList, ...tools]
}

function buildApplicationId(): string {
  const timestamp = Date.now().toString(36)
  const random = Math.random().toString(36).slice(2, 6)
  return `tool-${timestamp}-${random}`
}

export function AgentCoderWindowApp() {
  const { sendCommand, status: socketStatus } = useLocalBackendSocket()
  const [applications, setApplications] = useState<ApplicationOption[]>([])
  const [selectedApplicationId, setSelectedApplicationId] = useState('')
  const [newApplicationName, setNewApplicationName] = useState('')
  const [instruction, setInstruction] = useState(DEFAULT_INSTRUCTION)
  const [codePath, setCodePath] = useState('')
  const [codeDraft, setCodeDraft] = useState('')
  const [isDirty, setIsDirty] = useState(false)
  const [autoTest, setAutoTest] = useState(true)
  const [testOutput, setTestOutput] = useState('')
  const [diagnostic, setDiagnostic] = useState<RescueDiagnostic | null>(null)
  const [lastTestTargets, setLastTestTargets] = useState<string[]>([])
  const [operation, setOperation] = useState<Operation>('idle')
  const [message, setMessage] = useState('正在載入系統與應用程式清單...')

  const applicationsById = useMemo(
    () =>
      new Map(
        applications.map((application) => [application.id, application])
      ),
    [applications]
  )
  const selectedApplication = useMemo(
    () => applicationsById.get(selectedApplicationId),
    [applicationsById, selectedApplicationId]
  )
  const busy = operation !== 'idle'
  const draftLineCount = useMemo(
    () => (codeDraft ? codeDraft.split(/\r\n|\r|\n/).length : 0),
    [codeDraft]
  )
  const diagnosticTargets = diagnostic?.test_targets || lastTestTargets
  const diagnosticWarnings = diagnostic?.warnings || []
  const riskTone = diagnostic?.risk_level || 'unknown'

  const waitForIpcEvent = useCallback(
    (
      eventName: string,
      timeoutMs: number,
      options: { command?: string } = {}
    ): Promise<Record<string, unknown>> =>
      new Promise((resolve, reject) => {
        const timer = window.setTimeout(() => {
          window.removeEventListener('ipc_event', handler)
          reject(new Error(`等待事件逾時：${eventName}`))
        }, timeoutMs)

        const handler = (event: Event) => {
          const customEvent = event as CustomEvent
          const detail = customEvent.detail || {}
          const payload = (detail.payload || {}) as Record<string, unknown>
          if (detail.event !== eventName) {
            if (detail.event !== 'command_blocked_result') return
            const blockedCommand = String(payload.command || '')
            if (options.command && blockedCommand && blockedCommand !== options.command) {
              return
            }
          }
          window.clearTimeout(timer)
          window.removeEventListener('ipc_event', handler)
          resolve(payload)
        }

        window.addEventListener('ipc_event', handler)
      }),
    []
  )

  const waitForSocketReady = useCallback(
    (timeoutMs: number): Promise<boolean> => {
      if (socketStatus === 'Connected') return Promise.resolve(true)

      return new Promise((resolve) => {
        void window.electron?.invoke('app:ensure-backend-started').catch(() => {
          // The socket event below remains the source of truth.
        })

        const timer = window.setTimeout(() => {
          window.removeEventListener('socket_connected', handler)
          resolve(false)
        }, timeoutMs)

        const handler = (event: Event) => {
          const customEvent = event as CustomEvent<{ connected?: boolean }>
          if (!customEvent.detail?.connected) return
          window.clearTimeout(timer)
          window.removeEventListener('socket_connected', handler)
          resolve(true)
        }

        window.addEventListener('socket_connected', handler)
      })
    },
    [socketStatus]
  )

  const sendCommandAndWait = useCallback(
    async (
      command: string,
      eventName: string,
      payload: Record<string, unknown>,
      timeoutMs: number
    ): Promise<Record<string, unknown>> => {
      const connected = await waitForSocketReady(Math.min(timeoutMs, 12000))
      if (!connected) {
        throw new Error('後端連線尚未就緒，請稍後再試。')
      }

      const waitPromise = waitForIpcEvent(eventName, timeoutMs, { command })
      const sendResult = sendCommand(command, payload)
      if (!sendResult.ok && !sendResult.queued) {
        throw new Error(sendResult.message || `指令送出失敗：${command}`)
      }
      return waitPromise
    },
    [sendCommand, waitForIpcEvent, waitForSocketReady]
  )

  const diagnoseCode = useCallback(
    async (path: string, content: string, options: { quiet?: boolean } = {}) => {
      if (!path) return null
      try {
        const result = await sendCommandAndWait(
          'app:diagnose-code',
          'app:diagnose-code_result',
          {
            path,
            content,
          },
          10000
        )
        const nextDiagnostic = result as RescueDiagnostic
        setDiagnostic(nextDiagnostic)
        if (!options.quiet) {
          setMessage(
            nextDiagnostic.ok === false
              ? String(nextDiagnostic.message || '診斷失敗')
              : `診斷完成：${riskLabel(nextDiagnostic.risk_level)} (${formatClock(Date.now())})`
          )
        }
        return nextDiagnostic
      } catch (error) {
        const fallback = {
          ok: false,
          risk_level: 'warning',
          message: error instanceof Error ? error.message : '診斷失敗',
        }
        setDiagnostic(fallback)
        if (!options.quiet) setMessage(fallback.message)
        return fallback
      }
    },
    [sendCommandAndWait]
  )

  const refreshApplications = useCallback(async () => {
    setOperation('loading')
    try {
      const result = await sendCommandAndWait(
        'toolbox_list_tools',
        'toolbox_list_tools_result',
        { source: 'agent_coder_application' },
        10000
      )
      if (result.ok === false) {
        throw new Error(String(result.message || '載入應用程式失敗'))
      }

      const nextApplications = normalizeApplications(result.tools)
      setApplications(nextApplications)
      setSelectedApplicationId((current) =>
        nextApplications.some((application) => application.id === current)
          ? current
          : nextApplications[0]?.id || ''
      )
      setMessage(
        nextApplications.length > 0
          ? `應用程式清單已更新 (${formatClock(Date.now())})`
          : '目前沒有可編輯的應用程式，請先建立一個。'
      )
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '載入應用程式失敗')
    } finally {
      setOperation('idle')
    }
  }, [sendCommandAndWait])

  useEffect(() => {
    void refreshApplications()
  }, [refreshApplications])

  const openApplicationCode = useCallback(
    async (application: ApplicationOption): Promise<boolean> => {
      setOperation('opening')
      setMessage(`正在開啟 ${application.name} 程式碼...`)
      try {
        const result = await sendCommandAndWait(
          'toolbox_open_tool_code',
          'toolbox_open_tool_code_result',
          {
            tool_id: application.id,
            tool_name: application.name,
            no_external: true,
          },
          12000
        )
        if (result.ok === false) {
          throw new Error(String(result.message || '開啟應用程式程式碼失敗'))
        }

        const openedPath = String(result.file_path || '')
        const openedContent = String(result.content || '')
        setSelectedApplicationId(application.id)
        setCodePath(openedPath)
        setCodeDraft(openedContent)
        setIsDirty(false)
        setTestOutput('')
        setLastTestTargets([])
        void diagnoseCode(openedPath, openedContent, { quiet: true })
        setMessage(
          `已開啟 ${application.name} 程式碼 (${formatClock(Date.now())})`
        )
        return true
      } catch (error) {
        setMessage(
          error instanceof Error ? error.message : '開啟應用程式程式碼失敗'
        )
        return false
      } finally {
        setOperation('idle')
      }
    },
    [diagnoseCode, sendCommandAndWait]
  )

  const handleOpenCode = () => {
    if (!selectedApplication || busy) return
    void openApplicationCode(selectedApplication)
  }

  const handleCreateApplication = async () => {
    const applicationName = newApplicationName.trim()
    if (!applicationName || busy) return

    const application: ApplicationOption = {
      id: buildApplicationId(),
      name: applicationName,
      status: 'stopped',
    }
    setOperation('creating')
    setMessage(`正在建立 ${application.name}...`)
    try {
      const result = await sendCommandAndWait(
        'toolbox_add_tool',
        'toolbox_add_tool_result',
        {
          tool_id: application.id,
          tool_name: application.name,
        },
        15000
      )
      if (result.ok === false) {
        throw new Error(String(result.message || '建立應用程式失敗'))
      }

      setNewApplicationName('')
      await refreshApplications()
      await openApplicationCode(application)
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '建立應用程式失敗')
    } finally {
      setOperation('idle')
    }
  }

  const handleSaveCode = async (): Promise<boolean> => {
    if (!selectedApplication || !codePath || busy) return false

    setOperation('saving')
    setMessage(`正在儲存 ${selectedApplication.name} 程式碼...`)
    try {
      const result = await sendCommandAndWait(
        'toolbox_save_tool_code',
        'toolbox_save_tool_code_result',
        {
          tool_id: selectedApplication.id,
          tool_name: selectedApplication.name,
          content: codeDraft,
        },
        15000
      )
      if (result.ok === false) {
        throw new Error(String(result.message || '儲存程式碼失敗'))
      }

      setCodePath(String(result.file_path || codePath))
      setIsDirty(false)
      void diagnoseCode(String(result.file_path || codePath), codeDraft, {
        quiet: true,
      })
      setMessage(`程式碼已儲存 (${formatClock(Date.now())})`)
      return true
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '儲存程式碼失敗')
      return false
    } finally {
      setOperation('idle')
    }
  }

  const saveCodeRef = useRef(handleSaveCode)
  saveCodeRef.current = handleSaveCode

  const handleAgentIntervention = async () => {
    if (!selectedApplication || !codePath || busy) return

    setOperation('intervening')
    setMessage(`系統救援工具正在強制介入 ${selectedApplication.name}...`)
    try {
      const result = await sendCommandAndWait(
        'app:agent-intervention',
        'app:agent-intervention_result',
        {
          path: codePath,
          content: codeDraft,
        },
        120000
      )
      if (result.ok === false) {
        throw new Error(String(result.message || '系統救援工具介入失敗'))
      }

      const suggestedFix = result.suggested_fix || result.content
      if (typeof suggestedFix === 'string' && suggestedFix !== codeDraft) {
        setCodeDraft(suggestedFix)
        setIsDirty(true)
        void diagnoseCode(codePath, suggestedFix, { quiet: true })
      }
      const testOutputText = String(result.test_output || '')
      if (testOutputText) {
        const testResult =
          typeof result.test_result === 'object' && result.test_result
            ? (result.test_result as Record<string, unknown>)
            : {}
        setTestOutput(
          [formatTestSummary(testResult), testOutputText]
            .filter(Boolean)
            .join('\n\n')
        )
      }

      setMessage(
        `${String(result.message || '強制介入完成')} (${formatClock(Date.now())})`
      )
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '系統救援工具介入失敗')
    } finally {
      setOperation('idle')
    }
  }

  const handleRunUnitTests = async () => {
    if (busy) return

    setOperation('testing')
    setMessage('正在執行單元測試...')
    setTestOutput('')
    try {
      const result = await sendCommandAndWait(
        'app:run-unit-tests',
        'app:run-unit-tests_result',
        {
          path: codePath,
        },
        120000
      )
      const targets = Array.isArray(result.targets)
        ? result.targets.map((item) => String(item)).filter(Boolean)
        : []
      setLastTestTargets(targets)
      setTestOutput(
        [formatTestSummary(result), String(result.output || '')]
          .filter(Boolean)
          .join('\n\n')
      )
      if (result.ok === false) {
        throw new Error(String(result.message || '單元測試未通過'))
      }
      void diagnoseCode(codePath, codeDraft, { quiet: true })
      setMessage(`單元測試已通過 (${formatClock(Date.now())})`)
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '單元測試失敗')
    } finally {
      setOperation('idle')
    }
  }

  const handleSaveAndRunUnitTests = async () => {
    if (!selectedApplication || !codePath || busy) return

    const saved = isDirty ? await handleSaveCode() : true
    if (!saved) return

    await handleRunUnitTests()
  }

  const handleAgentReview = async (overrideInstruction?: string) => {
    if (!selectedApplication || !codePath || busy) return

    const reviewInstruction = (overrideInstruction ?? instruction).trim() || DEFAULT_INSTRUCTION
    setOperation('reviewing')
    setMessage(`系統救援工具正在處理 ${selectedApplication.name}...`)
    try {
      const result = await sendCommandAndWait(
        'app:agent-instruct',
        'app:agent-instruct_result',
        {
          path: codePath,
          content: codeDraft,
          instruction: reviewInstruction,
          auto_test: autoTest,
        },
        120000
      )
      if (result.ok === false) {
        throw new Error(String(result.message || '系統救援工具執行失敗'))
      }

      const suggestedFix = result.suggested_fix
      if (typeof suggestedFix === 'string' && suggestedFix !== codeDraft) {
        setCodeDraft(suggestedFix)
        setIsDirty(true)
        void diagnoseCode(codePath, suggestedFix, { quiet: true })
      }
      const testOutputText = String(result.test_output || '')
      if (testOutputText) {
        const testResult =
          typeof result.test_result === 'object' && result.test_result
            ? (result.test_result as Record<string, unknown>)
            : {}
        const targets = Array.isArray(testResult.targets)
          ? testResult.targets.map((item) => String(item)).filter(Boolean)
          : []
        setLastTestTargets(targets)
        setTestOutput(
          [formatTestSummary(testResult), testOutputText]
            .filter(Boolean)
            .join('\n\n')
        )
      }
      const testOk =
        typeof result.test_ok === 'boolean'
          ? result.test_ok
            ? '；自動測試通過'
            : '；自動測試未通過'
          : ''
      setMessage(
        `${String(result.message || '系統救援工具已完成處理')}${testOk} (${formatClock(Date.now())})`
      )
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '系統救援工具執行失敗')
    } finally {
      setOperation('idle')
    }
  }

  const handleManualDiagnose = () => {
    if (!codePath || busy) return
    void diagnoseCode(codePath, codeDraft)
  }

  const applyQuickInstruction = (value: string) => {
    setInstruction(value)
  }

  const diagnosticRows = [
    ['狀態', riskLabel(riskTone)],
    ['語言', diagnostic?.language || editorLanguage(codePath)],
    ['行數', draftLineCount.toLocaleString('zh-TW')],
    ['大小', formatBytes(diagnostic?.size_bytes)],
    ['測試', diagnosticTargets.length ? diagnosticTargets.join(', ') : '尚未判定'],
  ]

  return (
    <main className="agent-app">
      <header className="agent-app__header">
        <div>
          <p className="agent-app__eyebrow">GPTBridge Rescue</p>
          <h1>系統救援工具</h1>
        </div>
        <div className="agent-app__header-status">
          <span
            className={`agent-app__connection agent-app__connection--${socketStatus.toLowerCase()}`}
          >
            {socketStatus}
          </span>
          <span
            className={`agent-app__sync agent-app__sync--${isDirty ? 'dirty' : 'clean'}`}
          >
            {isDirty ? '未儲存' : '已同步'}
          </span>
        </div>
      </header>

      <section className="agent-app__workspace">
        <aside className="agent-app__sidebar">
          <section className={`agent-app__panel agent-app__panel--${riskTone}`}>
            <div className="agent-app__panel-head">
              <div>
                <span>救援診斷</span>
                <strong>{riskLabel(riskTone)}</strong>
              </div>
              <button
                type="button"
                onClick={handleManualDiagnose}
                disabled={busy || !codePath}
              >
                重新診斷
              </button>
            </div>
            <dl className="agent-app__diagnostics">
              {diagnosticRows.map(([label, value]) => (
                <div key={label}>
                  <dt>{label}</dt>
                  <dd>{value}</dd>
                </div>
              ))}
            </dl>
            {diagnosticWarnings.length ? (
              <ul className="agent-app__warning-list">
                {diagnosticWarnings.map((item) => (
                  <li key={item}>{item}</li>
                ))}
              </ul>
            ) : null}
            {diagnostic?.recommendations?.length ? (
              <p className="agent-app__hint">
                {diagnostic.recommendations[0]}
              </p>
            ) : null}
          </section>

          <section className="agent-app__panel">
            <div className="agent-app__panel-head">
              <div>
                <span>目標應用程式</span>
                <strong>{selectedApplication?.name || '尚未選擇'}</strong>
              </div>
              <button
                type="button"
                onClick={() => void refreshApplications()}
                disabled={busy}
              >
                重新整理
              </button>
            </div>

            <label>
              <select
                value={selectedApplicationId}
                onChange={(event) =>
                  setSelectedApplicationId(event.target.value)
                }
                disabled={busy || applications.length === 0}
              >
                {applications.length === 0 ? (
                  <option value="">尚無應用程式</option>
                ) : (
                  applications.map((application) => (
                    <option key={application.id} value={application.id}>
                      {application.name}
                    </option>
                  ))
                )}
              </select>
            </label>

            <div className="agent-app__create-row">
              <input
                value={newApplicationName}
                onChange={(event) => setNewApplicationName(event.target.value)}
                placeholder="新應用程式中文名稱"
                disabled={busy}
              />
              <button
                type="button"
                onClick={() => void handleCreateApplication()}
                disabled={busy || !newApplicationName.trim()}
              >
                建立
              </button>
            </div>
          </section>

          <section className="agent-app__panel">
            <div className="agent-app__panel-head">
              <div>
                <span>修補指令</span>
                <strong>指令控制</strong>
              </div>
            </div>

            <div className="agent-app__quick-actions">
              {QUICK_INSTRUCTIONS.map((item) => (
                <button
                  key={item.label}
                  type="button"
                  onClick={() => applyQuickInstruction(item.value)}
                  disabled={busy}
                >
                  {item.label}
                </button>
              ))}
            </div>

            <label>
              <textarea
                value={instruction}
                onChange={(event) => setInstruction(event.target.value)}
                placeholder={DEFAULT_INSTRUCTION}
                disabled={busy}
              />
            </label>

            <label className="agent-app__checkbox-label">
              <input
                type="checkbox"
                checked={autoTest}
                onChange={() => setAutoTest((current) => !current)}
                disabled={busy}
              />
              <span>修補後自動執行單元測試</span>
            </label>

            <div className="agent-app__action-row">
              <button
                type="button"
                className="agent-app__primary"
                onClick={() => void handleAgentReview()}
                disabled={busy || !codePath}
              >
                {operation === 'reviewing' ? '處理中...' : '交給系統救援工具'}
              </button>
              <button
                type="button"
                onClick={() => void handleAgentIntervention()}
                disabled={busy || !codePath}
              >
                {operation === 'intervening' ? '介入中...' : '強制介入'}
              </button>
            </div>
          </section>

          <section className="agent-app__status">
            <span className="agent-app__status-dot" />
            <p>{message}</p>
          </section>
        </aside>

        <section className="agent-app__editor">
          <div className="agent-app__editor-head">
            <div>
              <span>程式碼編輯器</span>
              <strong>{codePath || '請先開啟應用程式程式碼'}</strong>
            </div>
            <div className="agent-app__actions">
              <button
                type="button"
                onClick={handleOpenCode}
                disabled={busy || !selectedApplication}
              >
                開啟程式碼
              </button>
              <button
                type="button"
                onClick={() => void handleSaveCode()}
                disabled={busy || !codePath || !isDirty}
              >
                儲存程式碼
              </button>
              <button
                type="button"
                onClick={() => void handleSaveAndRunUnitTests()}
                disabled={busy || !codePath}
              >
                {operation === 'saving'
                  ? '儲存中...'
                  : operation === 'testing'
                    ? '測試中...'
                    : '儲存並測試'}
              </button>
              <button
                type="button"
                onClick={() => void handleRunUnitTests()}
                disabled={busy || !codePath}
              >
                {operation === 'testing' ? '測試中...' : '單元測試'}
              </button>
            </div>
          </div>

          <div className="agent-app__code">
            {!codePath ? (
              <div className="agent-app__empty-editor">
                開啟應用程式後，程式碼會顯示在這裡。
              </div>
            ) : (
              <Editor
                height="100%"
                theme="vs-dark"
                path={codePath}
                language={editorLanguage(codePath)}
                value={codeDraft}
                onChange={(value) => {
                  if (value !== undefined) {
                    setCodeDraft(value)
                    setIsDirty(true)
                  }
                }}
                onMount={(editor, monaco) => {
                  editor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS, () => {
                    void saveCodeRef.current()
                  })
                }}
                options={{
                  readOnly: busy,
                  minimap: { enabled: false },
                  scrollBeyondLastLine: false,
                  fontSize: 14,
                  wordWrap: 'on',
                  formatOnPaste: true,
                  formatOnType: true,
                  padding: { top: 16, bottom: 16 },
                }}
              />
            )}
          </div>

          <footer className="agent-app__editor-foot">
            <span>{isDirty ? '有未儲存變更' : '程式碼已同步'}</span>
            <span>{draftLineCount.toLocaleString('zh-TW')} 行</span>
            <span>{codeDraft.length.toLocaleString('zh-TW')} 字元</span>
          </footer>
        </section>
      </section>

      {testOutput ? (
        <section className="agent-app__results">
          <div className="agent-app__results-head">
            <div>
              <h2>測試結果</h2>
              <p>{lastTestTargets.length ? lastTestTargets.join(', ') : '最近一次執行結果'}</p>
            </div>
          </div>
          <pre>{testOutput}</pre>
        </section>
      ) : null}
    </main>
  )
}
