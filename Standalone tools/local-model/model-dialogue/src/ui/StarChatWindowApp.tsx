import { useEffect, useMemo, useRef, useState } from 'react'
import { useStarChatBackend } from './backendSocket'
import './star-chat.css'

type ReasoningLevel = 'light' | 'intermediate' | 'high-high' | 'ultra-high' | 'extreme'
type ReasoningEffort = 'low' | 'medium' | 'high'
type GenerationSpeed = 'slow' | 'low' | 'medium' | 'high' | 'ultra'
type TaskIntensity = 'simple' | 'normal' | 'intermediate' | 'difficult'
type ConversationMode = 'chat' | 'coding'
type Message = {
  id: string
  role: 'user' | 'assistant'
  content: string
  failed?: boolean
  model?: string
  execution?: string
  notice?: boolean
}
type ModelOption = {
  name: string
  label: string
  parameterCount: string
  quantization: string
  isDefault: boolean
  pickerGroup: string
}
const REASONING_EFFORT_BY_LEVEL: Record<ReasoningLevel, ReasoningEffort> = {
  light: 'low',
  intermediate: 'medium',
  'high-high': 'high',
  'ultra-high': 'high',
  extreme: 'high',
}
const GENERATION_SPEED_SETTINGS: Record<GenerationSpeed, { outputMultiplier: number; displayDelayMs: number }> = {
  slow: { outputMultiplier: 1.25, displayDelayMs: 34 },
  low: { outputMultiplier: 1.1, displayDelayMs: 26 },
  medium: { outputMultiplier: 1, displayDelayMs: 18 },
  high: { outputMultiplier: 0.75, displayDelayMs: 10 },
  ultra: { outputMultiplier: 0.5, displayDelayMs: 4 },
}
const TASK_INTENSITY_OUTPUT_TOKENS: Record<TaskIntensity, number> = {
  simple: 256,
  normal: 512,
  intermediate: 768,
  difficult: 1_024,
}
const HISTORY_BUDGET_BY_INTENSITY: Record<TaskIntensity, number> = {
  simple: 6,
  normal: 8,
  intermediate: 10,
  difficult: 12,
}

const DEFAULT_MODEL = 'gemma4:e2b-it-qat'
const PROGRAMMING_FOLDER_STORAGE_KEY = 'star-chat.programming-folder.v1'
const CONVERSATION_MODE_STORAGE_KEY = 'star-chat.conversation-mode.v1'
const AUTO_MODEL: ModelOption = {
  name: '',
  label: '自動模型路由（速度、推理與能力強度）',
  parameterCount: '智能路由',
  quantization: '',
  isDefault: false,
  pickerGroup: 'auto',
}
const STAR_NATIVE_MODEL: ModelOption = {
  name: 'star-main-native-model',
  label: '星澄（原生本地模型）',
  parameterCount: 'Star native',
  quantization: '原生統合引擎',
  isDefault: false,
  pickerGroup: 'native',
}
const GOVERNED_LOCAL_MODELS: ModelOption[] = [
  STAR_NATIVE_MODEL,
  {
    name: 'qwen3.5:9b-q4_K_M',
    label: 'Qwen3.5 9B · 命令理解與搜尋',
    parameterCount: '9.65B',
    quantization: 'Q4_K_M',
    isDefault: false,
    pickerGroup: 'search-agent',
  },
  {
    name: 'nemotron-3-nano:4b',
    label: 'Nemotron 3 Nano 4B · 命令理解備援',
    parameterCount: '4.0B',
    quantization: 'Q4_K_M',
    isDefault: false,
    pickerGroup: 'command-backup',
  },
  {
    name: DEFAULT_MODEL,
    label: 'Gemma 4 E2B · QAT 輕量',
    parameterCount: '2.3B effective / 5.1B total',
    quantization: 'QAT-4bit',
    isDefault: true,
    pickerGroup: 'daily:fast',
  },
  {
    name: 'qwen3.6:35b-a3b-coding',
    label: 'Qwen3.6 35B-A3B Coding · Agent',
    parameterCount: '35B total / 3B active',
    quantization: 'Q4_K_M',
    isDefault: false,
    pickerGroup: 'agent-code',
  },
  {
    name: 'qwen2.5-coder:7b',
    label: 'Qwen2.5 Coder 7B · 程式執行備援',
    parameterCount: '7.6B',
    quantization: 'Q4_K_M',
    isDefault: false,
    pickerGroup: 'code-backup',
  },
  {
    name: 'gpt-oss:20b',
    label: 'GPT-OSS 20B · 任務統合',
    parameterCount: '20.9B total / 3.6B active',
    quantization: 'MXFP4',
    isDefault: false,
    pickerGroup: 'coordinator',
  },
  {
    name: 'qwen3:30b-a3b-instruct-2507-q4_K_M',
    label: 'Qwen3 30B-A3B · 複雜規劃',
    parameterCount: '30.5B total / 3.3B active',
    quantization: 'Q4_K_M',
    isDefault: false,
    pickerGroup: 'complex',
  },
  {
    name: 'deepseek-r1:8b-0528-qwen3-q4_K_M',
    label: 'DeepSeek-R1 0528 8B · 計算推理',
    parameterCount: '8.19B',
    quantization: 'Q4_K_M',
    isDefault: false,
    pickerGroup: 'reasoning-medium',
  },
  {
    name: 'qwen3.8:27b-q4_K_M',
    label: 'Qwen3.8 27B · 執行檢查',
    parameterCount: '27.3B',
    quantization: 'Q4_K_M',
    isDefault: false,
    pickerGroup: 'advanced',
  },
  {
    name: 'llama3.1:8b-instruct-q4_K_M',
    label: 'Llama 3.1 8B · 手動備援',
    parameterCount: '8.03B',
    quantization: 'Q4_K_M',
    isDefault: false,
    pickerGroup: 'fallback',
  },
]
const DEFAULT_MODELS: ModelOption[] = [AUTO_MODEL, ...GOVERNED_LOCAL_MODELS]
const MODEL_GROUPS = [
  ['native', '星澄原生本地模型'],
  ['daily:fast', '一般日常與快速任務 · Gemma 4 E2B'],
  ['search-agent', '命令理解與搜尋 · Qwen3.5'],
  ['command-backup', '命令理解備援 · Nemotron 3 Nano'],
  ['complex', '複雜規劃 · Qwen3 30B-A3B'],
  ['reasoning-medium', '計算與中高推理 · DeepSeek-R1'],
  ['advanced', '執行檢查與高推理審查 · Qwen3.8'],
  ['agent-code', '全自動 Agent 與程式執行 · Qwen3.6'],
  ['code-backup', '快速程式執行備援 · Qwen2.5 Coder'],
  ['coordinator', '依序分工後統合 · GPT-OSS'],
  ['fallback', '備援／手動模型'],
  ['other', '其他已安裝模型'],
] as const

function responseText(result: Record<string, unknown>): string {
  if (typeof result.response === 'string' && result.response.trim()) return result.response.trim()
  if (typeof result.message === 'string' && result.message.trim()) return result.message.trim()
  return result.ok === false ? '所選模型目前無法完成這項要求。' : '模型已完成處理。'
}

function MessageContent({ text }: { text: string }) {
  const parts = useMemo(() => text.split(/(```[\s\S]*?```)/g), [text])
  return <>{parts.map((part, index) => {
    if (!part.startsWith('```')) return <span key={index}>{part}</span>
    const code = part.replace(/^```[^\n]*\n?/, '').replace(/```$/, '')
    return <pre key={index}><code>{code}</code></pre>
  })}</>
}

function progressiveChunks(text: string, size = 40): string[] {
  const characters = Array.from(text)
  const chunks: string[] = []
  for (let index = 0; index < characters.length; index += size) {
    chunks.push(characters.slice(index, index + size).join(''))
  }
  return chunks.length > 0 ? chunks : ['']
}

function localContextBudget(): { characters: number; memoryGb: number; cpuCores: number } {
  const navigatorWithMemory = navigator as Navigator & { deviceMemory?: number }
  const memoryGb = Number(navigatorWithMemory.deviceMemory || 8)
  const cpuCores = Number(navigator.hardwareConcurrency || 4)
  const constrained = memoryGb <= 4 || cpuCores <= 4
  const characters = constrained ? 6_000
    : memoryGb <= 8 || cpuCores <= 8 ? 10_000
      : memoryGb <= 16 ? 16_000
        : 20_000
  return { characters, memoryGb, cpuCores }
}

export function StarChatWindowApp() {
  const { request, cancelRequests, status } = useStarChatBackend()
  const [messages, setMessages] = useState<Message[]>([])
  const [draft, setDraft] = useState('')
  const [conversationMode, setConversationMode] = useState<ConversationMode>(() =>
    window.localStorage.getItem(CONVERSATION_MODE_STORAGE_KEY) === 'coding' ? 'coding' : 'chat'
  )
  const [pendingModeTransition, setPendingModeTransition] = useState<ConversationMode | null>(null)
  const [generating, setGenerating] = useState(false)
  const [modelReady, setModelReady] = useState<boolean | null>(null)
  const [models, setModels] = useState<ModelOption[]>(DEFAULT_MODELS)
  const [selectedModel, setSelectedModel] = useState('')
  const [reasoningLevel, setReasoningLevel] = useState<ReasoningLevel>('intermediate')
  const [generationSpeed, setGenerationSpeed] = useState<GenerationSpeed>('medium')
  const [taskIntensity, setTaskIntensity] = useState<TaskIntensity>('normal')
  const [thinkingSeconds, setThinkingSeconds] = useState(0)
  const [generationPhase, setGenerationPhase] = useState<'thinking' | 'responding'>('thinking')
  const [activeGenerationModel, setActiveGenerationModel] = useState('')
  const [activeStage, setActiveStage] = useState('準備處理')
  const [programmingFolder, setProgrammingFolder] = useState('')
  const [folderError, setFolderError] = useState('')
  const [scrolledAway, setScrolledAway] = useState(false)
  const messagesRef = useRef<HTMLDivElement | null>(null)
  const endRef = useRef<HTMLDivElement | null>(null)
  const streamSequenceRef = useRef(0)
  const connected = status === 'Connected'
  const thinkingModelLabel = activeGenerationModel || (selectedModel
    ? models.find((item) => item.name === selectedModel)?.label || selectedModel
    : '自動模型路由')
  const statusLabel = connected
    ? modelReady === false ? '服務已連線・模型未就緒' : '模型服務已連線'
    : status === 'Connecting' ? '等待連線' : '連線中斷・自動重試'

  useEffect(() => {
    let disposed = false
    let retryTimer: number | null = null
    if (!connected) {
      setModelReady(null)
      return () => { disposed = true }
    }
    const refreshModelStatus = async () => {
      try {
        const result = await request('star_chat_status', {}, 35_000)
        if (disposed) return
        const runtime = result.transformer_runtime
        const runtimeStatus = runtime && typeof runtime === 'object'
          ? runtime as Record<string, unknown>
          : null
        const ready = result.ok === true && runtimeStatus?.available === true &&
          runtimeStatus.model_installed === true &&
          Array.isArray(runtimeStatus.selectable_models) &&
          runtimeStatus.selectable_models.some((item) =>
            item && typeof item === 'object' && (item as Record<string, unknown>).installed === true
          )
        setModelReady(ready)
        const catalog = runtimeStatus?.selectable_models
        if (Array.isArray(catalog)) {
          const installedOptions = catalog.flatMap((item): ModelOption[] => {
            if (!item || typeof item !== 'object') return []
            const record = item as Record<string, unknown>
            const name = String(record.name || '').trim()
            if (!name || record.installed !== true) return []
            return [{
              name,
              label: String(record.label || name),
              parameterCount: String(record.parameter_count || ''),
              quantization: String(record.quantization || ''),
              isDefault: record.default === true,
              pickerGroup: ({
                'fast-intake-and-daily': 'daily:fast',
                complex: 'complex',
                'medium-reasoning': 'reasoning-medium',
                'advanced-reasoning': 'advanced',
                'autonomous-agent-and-command-execution': 'agent-code',
                'integration-coordinator': 'coordinator',
                'search-and-tool-coordinator': 'search-agent',
                'lightweight-fallback-and-manual': 'fallback',
              } as Record<string, string>)[String(record.usage_class || 'other')] || 'other',
            }]
          })
          if (installedOptions.length > 0) {
            const options = [AUTO_MODEL, STAR_NATIVE_MODEL, ...installedOptions]
            const backendDefault = installedOptions.find((item) => item.isDefault)
              || installedOptions.find((item) => item.name === DEFAULT_MODEL)
              || installedOptions[0]
            setModels(options)
            setSelectedModel((current) => options.some((item) => item.name === current)
              ? current
              : backendDefault.name)
          }
        }
        retryTimer = window.setTimeout(() => void refreshModelStatus(), ready ? 15_000 : 3_000)
      } catch {
        if (disposed) return
        setModelReady(false)
        retryTimer = window.setTimeout(() => void refreshModelStatus(), 3_000)
      }
    }
    void refreshModelStatus()
    return () => {
      disposed = true
      if (retryTimer !== null) window.clearTimeout(retryTimer)
    }
  }, [connected, request])

  useEffect(() => {
    if (!scrolledAway) endRef.current?.scrollIntoView({ behavior: generating ? 'auto' : 'smooth' })
  }, [messages, generating, scrolledAway])

  useEffect(() => {
    window.localStorage.setItem(CONVERSATION_MODE_STORAGE_KEY, conversationMode)
    setFolderError('')
  }, [conversationMode])

  useEffect(() => {
    const remembered = window.localStorage.getItem(PROGRAMMING_FOLDER_STORAGE_KEY)?.trim() || ''
    if (remembered) setProgrammingFolder(remembered)
  }, [])

  useEffect(() => {
    if (!generating) {
      setThinkingSeconds(0)
      return
    }
    setThinkingSeconds(0)
    const timer = window.setInterval(() => setThinkingSeconds((current) => current + 1), 1_000)
    return () => window.clearInterval(timer)
  }, [generating])

  const sendMessage = async () => {
    const message = draft.trim()
    if (!message || generating || !connected) return
    const historyBudget = HISTORY_BUDGET_BY_INTENSITY[taskIntensity]
    const history = messages.slice(-historyBudget).map(({ role, content }) => ({ role, content }))
    const assistantId = crypto.randomUUID()
    const streamSequence = streamSequenceRef.current + 1
    const speedSettings = GENERATION_SPEED_SETTINGS[generationSpeed]
    const maxOutputTokens = Math.round(
      TASK_INTENSITY_OUTPUT_TOKENS[taskIntensity] * speedSettings.outputMultiplier
    )
    const hardwareContext = localContextBudget()
    let streamed = false
    streamSequenceRef.current = streamSequence
    setMessages((current) => [
      ...current,
      { id: crypto.randomUUID(), role: 'user', content: message },
      { id: assistantId, role: 'assistant', content: '正在準備回覆…', notice: true },
    ])
    setDraft('')
    setGenerationPhase('thinking')
    setActiveGenerationModel('')
    setActiveStage('準備處理')
    setGenerating(true)
    try {
      const result = await request(
        'star_chat_send_message',
        {
          message,
          history,
          max_output_tokens: maxOutputTokens,
          runtime_model: selectedModel,
          reasoning_level: reasoningLevel,
          reasoning_effort: REASONING_EFFORT_BY_LEVEL[reasoningLevel],
          generation_speed: generationSpeed,
          task_intensity: taskIntensity,
          conversation_mode: conversationMode,
          previous_conversation_mode: pendingModeTransition,
          context_budget_characters: hardwareContext.characters,
          local_hardware_profile: {
            memory_gb: hardwareContext.memoryGb,
            cpu_cores: hardwareContext.cpuCores,
          },
          autonomous_agent: true,
          programming_folder: conversationMode === 'coding' ? programmingFolder : '',
        },
        610_000,
        (progress) => {
          const text = String(progress.text || '')
          const progressModel = String(progress.model || '')
          const phase = String(progress.phase || '')
          const stageLabel = ({
            'command-understanding': '繁中命令理解',
            'workflow-frontend': '流程規劃',
            'command-planned': '專家模型處理',
            'generating': '生成回答',
          } as Record<string, string>)[phase] || String(progress.message || '') || '處理中'
          setActiveStage(stageLabel)
          if (progressModel) {
            setActiveGenerationModel(models.find((item) => item.name === progressModel)?.label || progressModel)
          }
          if (!text || streamSequenceRef.current !== streamSequence) return
          streamed = true
          setGenerationPhase('responding')
          setMessages((current) => current.some((item) => item.id === assistantId)
            ? current.map((item) => item.id === assistantId
              ? { ...item, content: text, model: progressModel, notice: false }
              : item)
            : [...current, { id: assistantId, role: 'assistant', content: text, model: progressModel }])
        }
      )
      const model = result.generation && typeof result.generation === 'object'
        ? String((result.generation as Record<string, unknown>).model || '')
        : String(result.model || '')
      const completeText = responseText(result)
      const instruction = result.instruction_execution && typeof result.instruction_execution === 'object'
        ? result.instruction_execution as Record<string, unknown>
        : null
      const execution = instruction?.executed === true
        ? '命令已理解並執行'
        : instruction?.status === 'input-required'
          ? '命令已理解・需要補充輸入'
          : ''
      setPendingModeTransition(null)
      setActiveGenerationModel(models.find((item) => item.name === model)?.label || model)
      setGenerationPhase('responding')
      setMessages((current) => {
        const finalMessage: Message = {
          id: assistantId,
          role: 'assistant',
          content: result.ok === false || streamed ? completeText : '',
          failed: result.ok === false,
          model,
          execution,
        }
        return current.some((item) => item.id === assistantId)
          ? current.map((item) => item.id === assistantId ? finalMessage : item)
          : [...current, finalMessage]
      })
      if (result.ok !== false && !streamed) {
        for (const chunk of progressiveChunks(completeText)) {
          if (streamSequenceRef.current !== streamSequence) break
          setMessages((current) => current.map((item) => item.id === assistantId
            ? { ...item, content: item.content + chunk }
            : item))
          await new Promise((resolve) => window.setTimeout(resolve, speedSettings.displayDelayMs))
        }
      }
    } catch (error) {
      const content = error instanceof Error ? error.message : '無法取得所選模型的回應。'
      setMessages((current) => current.map((item) => item.id === assistantId ? {
        ...item,
        content,
        notice: false,
        failed: content !== '已停止產生回答。',
      } : item))
    } finally {
      setGenerating(false)
    }
  }

  const stopGenerating = () => {
    if (!generating) return
    streamSequenceRef.current += 1
    cancelRequests('star_chat_send_message')
    setGenerating(false)
  }

  const selectProgrammingFolder = async () => {
    const selected = String(
      (await (window as any).electron?.invoke?.('dialog:select-folder')) || ''
    ).trim()
    if (!selected) return
    setFolderError('')
    setProgrammingFolder(selected)
    window.localStorage.setItem(PROGRAMMING_FOLDER_STORAGE_KEY, selected)
  }

  const switchConversationMode = (nextMode: ConversationMode) => {
    if (nextMode === conversationMode || generating) return
    const previousMode = conversationMode
    setConversationMode(nextMode)
    setPendingModeTransition(previousMode)
    if (connected) {
      void request('star_chat_status', { prepare_mode: nextMode }, 120_000).catch(() => undefined)
    }
    setMessages((current) => [...current, {
      id: crypto.randomUUID(),
      role: 'assistant',
      content: `已切換為 ${nextMode === 'chat' ? 'Chat' : 'Coding'} 模式；下次送出時會先通知模型完成角色切換。`,
      notice: true,
    }])
  }

  return (
    <div className="star-shell" data-conversation-mode={conversationMode}>
      <aside className="star-sidebar">
        <div className="brand"><span className="brand-mark">模</span><div><strong>模型對話</strong><small>本機模型對話入口</small></div></div>
        <nav aria-label="主要功能"><button className="active"><span>✦</span> 模型對話</button></nav>
        <div className="sidebar-info">
          <div className={`connection-dot ${connected ? 'online' : ''}`} />
          <div><strong>{statusLabel}</strong><small>{models.find((item) => item.name === selectedModel)?.parameterCount || '本機'} 模型 · v1.0</small></div>
        </div>
        <div className="privacy-note">外部協作已停用<br />訓練與能力編成由星澄原生模型內部自行處理</div>
      </aside>

      <main className="star-main">
        <header>
          <div className="title-block"><span className="eyebrow">GOVERNED LOCAL INTELLIGENCE</span><h1>模型對話</h1>
            <div className="conversation-mode-switch" role="group" aria-label="對話模式">
              <button type="button" className={conversationMode === 'chat' ? 'active' : ''} aria-pressed={conversationMode === 'chat'} onClick={() => switchConversationMode('chat')} disabled={generating}><span>Chat</span><small>一般對話</small></button>
              <button type="button" className={conversationMode === 'coding' ? 'active' : ''} aria-pressed={conversationMode === 'coding'} onClick={() => switchConversationMode('coding')} disabled={generating}><span>Coding</span><small>程式工作</small></button>
            </div>
          </div>
          <div className="header-actions">
            {conversationMode === 'coding' && <label className="programming-folder-picker">
              <span>Coding 作業頂層資料夾</span>
              <div><input value={programmingFolder} readOnly title={programmingFolder} placeholder="尚未設定" /><button type="button" onClick={() => void selectProgrammingFolder()} disabled={generating}>設定</button></div>
              <small className="folder-scan-summary">{programmingFolder ? '程式作業範圍限制於此資料夾' : '未設定時使用預設工作區，仍可執行編程任務'}</small>
              {folderError && <small>{folderError}</small>}
            </label>}
            <details className="advanced-settings">
              <summary>進階設定 <small>{models.find((item) => item.name === selectedModel)?.label || '自動模型'}</small></summary>
              <div className="advanced-settings-panel">
            <label className="model-picker compact-picker"><span>推理等級</span><select aria-label="選擇推理等級" value={reasoningLevel} onChange={(event) => setReasoningLevel(event.target.value as ReasoningLevel)} disabled={generating || !connected}>
              <option value="light">輕度</option>
              <option value="intermediate">中級</option>
              <option value="high-high">高高</option>
              <option value="ultra-high">超高</option>
              <option value="extreme">極高</option>
            </select></label>
            <label className="model-picker compact-picker"><span>反應速度</span><select aria-label="選擇模型反應速度" value={generationSpeed} onChange={(event) => setGenerationSpeed(event.target.value as GenerationSpeed)} disabled={generating || !connected}>
              <option value="slow">慢速</option>
              <option value="low">低速</option>
              <option value="medium">中速</option>
              <option value="high">高速</option>
              <option value="ultra">超高速</option>
            </select></label>
            <label className="model-picker compact-picker"><span>任務強度</span><select aria-label="選擇任務強度" value={taskIntensity} onChange={(event) => setTaskIntensity(event.target.value as TaskIntensity)} disabled={generating || !connected}>
              <option value="simple">簡單</option>
              <option value="normal">普通</option>
              <option value="intermediate">中級</option>
              <option value="difficult">困難</option>
            </select></label>
            <label className="model-picker"><span>生成模型</span><select aria-label="選擇生成模型" value={selectedModel} onChange={(event) => setSelectedModel(event.target.value)} disabled={generating || !connected}>
              <option value="">{AUTO_MODEL.label}</option>
              {MODEL_GROUPS.map(([group, label]) => {
                const options = models.filter((model) => model.name && model.pickerGroup === group)
                return options.length > 0 ? <optgroup key={group} label={label}>
                  {options.map((model) => <option key={model.name} value={model.name}>{model.label}{model.quantization ? ` · ${model.quantization}` : ''}</option>)}
                </optgroup> : null
              })}
            </select></label>
              </div>
            </details>
            <button className="ghost-button" onClick={() => setMessages([])} disabled={generating || messages.length === 0}>清除本次對話</button>
          </div>
        </header>

        <section className="chat-workspace">
          <div ref={messagesRef} className="messages" aria-live="polite" onScroll={(event) => {
            const element = event.currentTarget
            setScrolledAway(element.scrollHeight - element.scrollTop - element.clientHeight > 96)
          }}>
            {messages.length === 0 && (
              <div className="welcome">
                <div className="welcome-orbit"><span>模</span></div>
                <h2>{conversationMode === 'chat' ? 'Chat 模式，現在可以開始聊聊。' : 'Coding 模式，準備處理程式任務。'}</h2>
                <p>{conversationMode === 'chat' ? '適合問答、討論、整理想法與撰寫內容；模型會以自然對話方式回應。' : '選擇編程資料夾後，模型會以程式工程語境分析、編寫、檢查與修正程式。'}</p>
                <div className="suggestions">
                  {(conversationMode === 'chat'
                    ? ['幫我整理今天的想法', '用簡單方式解釋一個概念', '幫我潤飾這段文字']
                    : ['幫我把需求拆成可執行步驟', '設計一個 Python API 並附測試', '依照我的命令編寫、檢查並修正程式']).map((item) => (
                    <button key={item} onClick={() => setDraft(item)}>{item}</button>
                  ))}
                </div>
              </div>
            )}
            {messages.map((message) => (
              <article key={message.id} className={`message ${message.role} ${message.failed ? 'failed' : ''} ${message.notice ? 'mode-notice' : ''}`}>
                <div className="avatar">{message.role === 'assistant' ? '模' : '你'}</div>
                <div><small>{message.role === 'assistant'
                  ? (models.find((item) => item.name === (message.model || selectedModel))?.label || message.model || '模型')
                  : '你'}</small><p><MessageContent text={message.content} /></p>{message.execution && <span className="execution-status">{message.execution}</span>}</div>
              </article>
            ))}
            {generating && <article className="message assistant"><div className="avatar">模</div><div><small>目前模型：{thinkingModelLabel}</small><div className="thinking"><i /><i /><i /><span className="thinking-copy"><strong>{activeStage}</strong><small>{thinkingModelLabel} · 已處理 {thinkingSeconds} 秒{generationPhase === 'responding' ? ' · 正在輸出回答' : ''}</small></span></div></div></article>}
            <div ref={endRef} />
            {scrolledAway && <button className="jump-to-latest" type="button" onClick={() => {
              endRef.current?.scrollIntoView({ behavior: 'smooth' })
              setScrolledAway(false)
            }}>↓ 回到最新訊息</button>}
          </div>
          <div className="composer-wrap">
            {!connected && <div className="connection-banner"><span className="spinner" />後端連線等待中，連線完成後即可送出。</div>}
            <div className="composer">
              <textarea aria-label="傳送訊息給所選模型" value={draft} onChange={(event) => setDraft(event.target.value)} onKeyDown={(event) => {
                if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); void sendMessage() }
              }} placeholder={conversationMode === 'chat' ? '輸入想聊的內容…' : '輸入程式需求、錯誤訊息或開發指令…'} disabled={generating} />
              {generating
                ? <button className="stop-button" aria-label="停止產生回答" title="停止產生回答" onClick={stopGenerating}>■</button>
                : <button aria-label="送出訊息" onClick={() => void sendMessage()} disabled={!draft.trim() || !connected}>↑</button>}
            </div>
            <small className="composer-hint">{conversationMode === 'chat' ? 'Chat 一般對話' : 'Coding 程式工作'} · Enter 送出 · Shift + Enter 換行 · 上下文依本機負載自動調整</small>
          </div>
        </section>
      </main>
    </div>
  )
}
