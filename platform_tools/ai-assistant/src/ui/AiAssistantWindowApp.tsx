import { useCallback, useEffect, useMemo, useState } from 'react'
import { useLocalBackendSocket } from './backendSocket'
import './ai-assistant.css'

type AssistMode =
  | 'gpt_first'
  | 'gemini_first'
  | 'ask_both'
  | 'all_ai'
  | 'claude_first'
  | 'perplexity_first'
  | 'deepseek_first'

type ProviderKey = 'chatgpt' | 'gemini' | 'claude' | 'perplexity' | 'deepseek'

const AI_DRAFT_KEY = 'gptbridge_ai_assistant_draft'
const AI_CONTEXT_KEY = 'gptbridge_ai_assistant_context'
const ALL_PROVIDERS: ProviderKey[] = [
  'chatgpt',
  'gemini',
  'claude',
  'perplexity',
  'deepseek',
]
const BROWSER_ONLY_PROVIDERS: ProviderKey[] = [
  'claude',
  'perplexity',
  'deepseek',
]
const AI_RULES = [
  '請用繁體中文回答。',
  '先指出最可能的根因，再給可執行的修補步驟。',
  '涉及程式碼時，請保留必要上下文，避免只回覆片段。',
  '不確定時要明確標示假設，不要假裝已驗證。',
].join('\n')

function formatClock(timestamp: number): string {
  return new Date(timestamp).toLocaleTimeString('zh-TW', { hour12: false })
}

function assistLabel(mode: AssistMode): string {
  if (mode === 'gpt_first') return 'GPT 優先'
  if (mode === 'gemini_first') return 'Gemini 優先'
  if (mode === 'ask_both') return 'ChatGPT + Gemini'
  if (mode === 'all_ai') return '同時詢問全 AI'
  if (mode === 'claude_first') return 'Claude'
  if (mode === 'perplexity_first') return 'Perplexity'
  return 'DeepSeek'
}

function providerLabel(provider: ProviderKey): string {
  if (provider === 'chatgpt') return 'ChatGPT'
  if (provider === 'gemini') return 'Gemini'
  if (provider === 'claude') return 'Claude'
  if (provider === 'perplexity') return 'Perplexity'
  return 'DeepSeek'
}

function backendMode(mode: AssistMode): 'chatgpt_first' | 'gemini_first' | 'ask_both' {
  if (mode === 'gemini_first') return 'gemini_first'
  if (mode === 'ask_both' || mode === 'all_ai') return 'ask_both'
  return 'chatgpt_first'
}

function manualProvider(mode: AssistMode): ProviderKey | null {
  if (mode === 'claude_first') return 'claude'
  if (mode === 'perplexity_first') return 'perplexity'
  if (mode === 'deepseek_first') return 'deepseek'
  return null
}

function requiredProviders(mode: AssistMode): ProviderKey[] {
  if (mode === 'gpt_first') return ['chatgpt']
  if (mode === 'gemini_first') return ['gemini']
  if (mode === 'ask_both') return ['chatgpt', 'gemini']
  if (mode === 'all_ai') return ALL_PROVIDERS
  const provider = manualProvider(mode)
  return provider ? [provider] : ['chatgpt']
}

function isProviderReady(status: unknown): boolean {
  const value = String(status ?? '').toUpperCase()
  return value === 'AUTHENTICATED' || value === 'READY' || value === 'UNOPENED'
}

function buildPrompt(draft: string, context: string): string {
  const parts = [AI_RULES, draft.trim()]
  const trimmedContext = context.trim()
  if (trimmedContext) {
    parts.push(`參考內容：\n${trimmedContext}`)
  }
  return parts.filter(Boolean).join('\n\n')
}

export function AiAssistantWindowApp() {
  const { sendCommand, status: socketStatus } = useLocalBackendSocket()
  const [assistMode, setAssistMode] = useState<AssistMode>('gpt_first')
  const [draft, setDraft] = useState('')
  const [context, setContext] = useState('')
  const [answer, setAnswer] = useState('')
  const [message, setMessage] = useState('AI 協作工具已就緒')
  const [busy, setBusy] = useState(false)
  const [urlDraft, setUrlDraft] = useState({
    chatgpt_main_url: '',
    gemini_main_url: '',
    claude_main_url: '',
    perplexity_main_url: '',
    deepseek_main_url: '',
  })
  const [baseConfig, setBaseConfig] = useState<Record<string, unknown>>({})

  const prompt = useMemo(() => buildPrompt(draft, context), [context, draft])

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
            if (
              options.command &&
              blockedCommand &&
              blockedCommand !== options.command
            ) {
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
      if (!connected) throw new Error('後端尚未連線，請稍後再試。')

      const waitPromise = waitForIpcEvent(eventName, timeoutMs, { command })
      const sendResult = sendCommand(command, payload)
      if (!sendResult.ok && !sendResult.queued) {
        throw new Error(sendResult.message || `送出命令失敗：${command}`)
      }
      return waitPromise
    },
    [sendCommand, waitForIpcEvent, waitForSocketReady]
  )

  const loadConfig = useCallback(async () => {
    try {
      const result = await sendCommandAndWait(
        'load_config',
        'load_config_result',
        {},
        10000
      )
      if (result.ok === false) return
      const config = (result.config as Record<string, unknown>) || {}
      setBaseConfig(config)
      setUrlDraft({
        chatgpt_main_url: String(config.chatgpt_main_url ?? ''),
        gemini_main_url: String(config.gemini_main_url ?? ''),
        claude_main_url: String(config.claude_main_url ?? ''),
        perplexity_main_url: String(config.perplexity_main_url ?? ''),
        deepseek_main_url: String(config.deepseek_main_url ?? ''),
      })
    } catch {
      // Keep the AI panel usable even if settings are not ready yet.
    }
  }, [sendCommandAndWait])

  useEffect(() => {
    try {
      setDraft(localStorage.getItem(AI_DRAFT_KEY) || '')
      setContext(localStorage.getItem(AI_CONTEXT_KEY) || '')
    } catch {
      // Ignore localStorage restriction in locked environments.
    }
    void loadConfig()
  }, [loadConfig])

  useEffect(() => {
    try {
      localStorage.setItem(AI_DRAFT_KEY, draft)
    } catch {
      // Ignore localStorage restriction in locked environments.
    }
  }, [draft])

  useEffect(() => {
    try {
      localStorage.setItem(AI_CONTEXT_KEY, context)
    } catch {
      // Ignore localStorage restriction in locked environments.
    }
  }, [context])

  const saveConfig = async () => {
    const trimmed = {
      chatgpt_main_url: urlDraft.chatgpt_main_url.trim(),
      gemini_main_url: urlDraft.gemini_main_url.trim(),
      claude_main_url: urlDraft.claude_main_url.trim(),
      perplexity_main_url: urlDraft.perplexity_main_url.trim(),
      deepseek_main_url: urlDraft.deepseek_main_url.trim(),
    }
    const invalid = Object.values(trimmed).some(
      (value) => value.length > 0 && !/^https?:\/\//i.test(value)
    )
    if (invalid) {
      setMessage('URL 必須以 http:// 或 https:// 開頭。')
      return
    }

    setBusy(true)
    setMessage('正在儲存 URL 設定...')
    try {
      const result = await sendCommandAndWait(
        'save_config',
        'save_config_result',
        { config: { ...baseConfig, ...trimmed } },
        15000
      )
      if (result.ok === false) {
        throw new Error(String(result.message || '儲存設定失敗'))
      }
      setMessage(`URL 設定已儲存 (${formatClock(Date.now())})`)
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '儲存設定失敗')
    } finally {
      setBusy(false)
    }
  }

  const openProvider = async (
    provider: ProviderKey,
    options: { manageBusy?: boolean; quiet?: boolean } = {}
  ) => {
    const manageBusy = options.manageBusy ?? true
    if (manageBusy) setBusy(true)
    if (!options.quiet) setMessage(`正在開啟 ${providerLabel(provider)}...`)
    try {
      const result = await sendCommandAndWait(
        'settings_open_system_browser',
        'settings_open_system_browser_result',
        { provider },
        10000
      )
      if (result.ok === false) {
        throw new Error(String(result.message || '開啟 AI 瀏覽器失敗'))
      }
      if (!options.quiet) setMessage(`${providerLabel(provider)} 已開啟`)
    } catch (error) {
      if (!options.quiet) {
        setMessage(error instanceof Error ? error.message : '開啟 AI 瀏覽器失敗')
      }
      throw error
    } finally {
      if (manageBusy) setBusy(false)
    }
  }

  const ensureProvidersReady = async (): Promise<boolean> => {
    const providers = requiredProviders(assistMode)
    const manual = manualProvider(assistMode)
    if (manual) {
      await openProvider(manual)
      return true
    }

    const browserOnlyProviders = providers.filter((provider) =>
      BROWSER_ONLY_PROVIDERS.includes(provider)
    )
    if (browserOnlyProviders.length > 0) {
      setMessage(
        `正在開啟 ${browserOnlyProviders
          .map((provider) => providerLabel(provider))
          .join('、')}...`
      )
      for (const provider of browserOnlyProviders) {
        await openProvider(provider, { manageBusy: false, quiet: true })
      }
    }

    const backendProviders = providers.filter(
      (provider) => provider === 'chatgpt' || provider === 'gemini'
    )
    if (backendProviders.length === 0) return true

    const result = await sendCommandAndWait(
      'mother_provider_status',
      'mother_provider_status_result',
      { source: 'ai_assistant' },
      9000
    )
    if (result.ok === false) return false

    const chatgptReady = !backendProviders.includes('chatgpt') || isProviderReady(result.chatgpt_status)
    const geminiReady = !backendProviders.includes('gemini') || isProviderReady(result.gemini_status)
    if (chatgptReady && geminiReady) return true

    if (!chatgptReady) sendCommand('settings_open_system_browser', { provider: 'chatgpt' })
    if (!geminiReady) sendCommand('settings_open_system_browser', { provider: 'gemini' })
    setMessage('AI 瀏覽器尚未就緒，已開啟 Edge，請完成登入後再送出。')
    return false
  }

  const submitToAi = async () => {
    if (busy) return
    if (!draft.trim()) {
      setMessage('請先輸入 AI 需求。')
      return
    }

    setBusy(true)
    setAnswer('')
    setMessage('正在送出 AI 需求...')
    try {
      let allAiClipboardReady = false
      if (assistMode === 'all_ai') {
        try {
          await navigator.clipboard.writeText(prompt)
          allAiClipboardReady = true
        } catch {
          allAiClipboardReady = false
        }
      }

      const manual = manualProvider(assistMode)
      if (manual) {
        await openProvider(manual)
        await navigator.clipboard.writeText(prompt)
        setAnswer(prompt)
        setMessage(
          `${providerLabel(manual)} 已開啟，完整提示已複製到剪貼簿。`
        )
        return
      }

      const ready = await ensureProvidersReady()
      if (!ready) {
        if (assistMode === 'all_ai') {
          setAnswer(prompt)
          setMessage(
            allAiClipboardReady
              ? '已開啟全 AI 瀏覽器，完整提示已複製；請完成 ChatGPT/Gemini 登入後再送出。'
              : '已開啟全 AI 瀏覽器；剪貼簿不可用，請從回答內容複製完整提示。'
          )
        }
        return
      }

      const result = await sendCommandAndWait(
        'discussion_query',
        'discussion_result',
        {
          text: prompt,
          mode: backendMode(assistMode),
          source: 'ai_assistant',
        },
        45000
      )
      if (result.ok === false) {
        throw new Error(String(result.message || 'AI 回答失敗'))
      }
      const finalText = String(
        result.final_summary || result.message || '目前沒有可顯示的 AI 回答。'
      )
      if (assistMode === 'all_ai') {
        const manualLabels = BROWSER_ONLY_PROVIDERS.map((provider) =>
          providerLabel(provider)
        ).join('、')
        setAnswer(
          [
            finalText,
            '',
            '---',
            `已同時開啟：${manualLabels}`,
            allAiClipboardReady
              ? '完整提示已複製到剪貼簿，可貼到已開啟的 AI 對話框。'
              : '剪貼簿不可用，請手動複製完整提示貼到已開啟的 AI 對話框。',
          ].join('\n')
        )
        setMessage(`全 AI 已送出/開啟 (${formatClock(Date.now())})`)
      } else {
        setAnswer(finalText)
        setMessage(`AI 回答已更新 (${formatClock(Date.now())})`)
      }
    } catch (error) {
      setMessage(error instanceof Error ? error.message : 'AI 送出失敗')
    } finally {
      setBusy(false)
    }
  }

  const copyPrompt = async () => {
    if (!draft.trim()) {
      setMessage('目前沒有可複製的 AI 需求。')
      return
    }
    try {
      await navigator.clipboard.writeText(prompt)
      setMessage(`AI 需求已複製 (${formatClock(Date.now())})`)
    } catch {
      setMessage('複製失敗，請手動選取內容。')
    }
  }

  return (
    <main className="ai-app">
      <header className="ai-app__header">
        <div>
          <p className="ai-app__eyebrow">GPTBridge Application</p>
          <h1>AI 協作工具</h1>
          <p>集中管理 AI 登入、提示內容與多來源回覆。</p>
        </div>
        <span className={`ai-app__connection ai-app__connection--${socketStatus.toLowerCase()}`}>
          {socketStatus}
        </span>
      </header>

      <section className="ai-app__layout">
        <aside className="ai-app__settings">
          <section className="ai-app__panel">
            <div className="ai-app__panel-head">
              <span>AI 來源</span>
              <strong>{assistLabel(assistMode)}</strong>
            </div>
            <select
              value={assistMode}
              onChange={(event) => setAssistMode(event.target.value as AssistMode)}
              disabled={busy}
            >
              <option value="gpt_first">GPT 優先</option>
              <option value="gemini_first">Gemini 優先</option>
              <option value="ask_both">ChatGPT + Gemini</option>
              <option value="all_ai">同時詢問全 AI</option>
              <option value="claude_first">Claude</option>
              <option value="perplexity_first">Perplexity</option>
              <option value="deepseek_first">DeepSeek</option>
            </select>
          </section>

          <section className="ai-app__panel">
            <div className="ai-app__panel-head">
              <span>URL 與登入</span>
              <button type="button" onClick={() => void loadConfig()} disabled={busy}>
                重新載入
              </button>
            </div>
            <div className="ai-app__url-grid">
              <input
                type="url"
                placeholder="ChatGPT URL"
                value={urlDraft.chatgpt_main_url}
                onChange={(event) =>
                  setUrlDraft((current) => ({
                    ...current,
                    chatgpt_main_url: event.target.value,
                  }))
                }
                disabled={busy}
              />
              <input
                type="url"
                placeholder="Gemini URL"
                value={urlDraft.gemini_main_url}
                onChange={(event) =>
                  setUrlDraft((current) => ({
                    ...current,
                    gemini_main_url: event.target.value,
                  }))
                }
                disabled={busy}
              />
              <input
                type="url"
                placeholder="Claude URL"
                value={urlDraft.claude_main_url}
                onChange={(event) =>
                  setUrlDraft((current) => ({
                    ...current,
                    claude_main_url: event.target.value,
                  }))
                }
                disabled={busy}
              />
              <input
                type="url"
                placeholder="Perplexity URL"
                value={urlDraft.perplexity_main_url}
                onChange={(event) =>
                  setUrlDraft((current) => ({
                    ...current,
                    perplexity_main_url: event.target.value,
                  }))
                }
                disabled={busy}
              />
              <input
                type="url"
                placeholder="DeepSeek URL"
                value={urlDraft.deepseek_main_url}
                onChange={(event) =>
                  setUrlDraft((current) => ({
                    ...current,
                    deepseek_main_url: event.target.value,
                  }))
                }
                disabled={busy}
              />
            </div>
            <div className="ai-app__button-grid">
              <button type="button" className="ai-app__primary" onClick={() => void saveConfig()} disabled={busy}>
                儲存 URL
              </button>
              {(['chatgpt', 'gemini', 'claude', 'perplexity', 'deepseek'] as ProviderKey[]).map(
                (provider) => (
                  <button
                    key={provider}
                    type="button"
                    onClick={() => void openProvider(provider)}
                    disabled={busy}
                  >
                    登入 {providerLabel(provider)}
                  </button>
                )
              )}
            </div>
          </section>
        </aside>

        <section className="ai-app__workspace">
          <section className="ai-app__panel ai-app__composer">
            <div className="ai-app__panel-head">
              <span>AI 需求</span>
              <strong>{draft.length.toLocaleString('zh-TW')} 字</strong>
            </div>
            <textarea
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              placeholder="輸入需求、錯誤訊息、想請 AI 協助整理或分析的內容..."
              disabled={busy}
            />
            <textarea
              className="ai-app__context"
              value={context}
              onChange={(event) => setContext(event.target.value)}
              placeholder="可選：貼上程式碼、日誌或額外背景..."
              disabled={busy}
            />
            <div className="ai-app__actions">
              <button
                type="button"
                className="ai-app__primary"
                onClick={() => void submitToAi()}
                disabled={busy}
              >
                {busy ? '處理中...' : '送出 AI'}
              </button>
              <button type="button" onClick={() => void copyPrompt()} disabled={busy || !draft.trim()}>
                複製完整提示
              </button>
            </div>
            <p className="ai-app__message">{message}</p>
          </section>

          <section className="ai-app__panel ai-app__answer">
            <div className="ai-app__panel-head">
              <span>回答內容</span>
              <strong>{answer ? formatClock(Date.now()) : '待回覆'}</strong>
            </div>
            <pre>{answer || '送出 AI 需求後，回答會顯示在這裡。'}</pre>
          </section>
        </section>
      </section>
    </main>
  )
}
