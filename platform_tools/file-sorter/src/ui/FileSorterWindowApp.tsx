import { useEffect, useMemo, useRef, useState } from 'react'
import {
  formatFileSize,
  formatRunOutput,
  isAbsoluteFilesystemPath,
  openPath,
  parseToolJson,
  selectFolder,
  toolWindowStyles as styles,
  useToolRunner,
  type ToolRunResult,
  type ToolRunOptions,
} from './toolWindowRunner'

type RunState = 'idle' | 'running' | 'success' | 'error'

const TOOL_ID = 'file-sorter'
const FOLDERS_JSON_PREFIX = 'FILE_SORTER_FOLDERS_JSON='

type CleanupFile = {
  path: string
  categories?: string[]
  size?: number | null
  similar_to?: string
  video_similarity?: number
  perceptual_distance?: number
  video_issue?: string
  width?: number
  height?: number
  aspect_ratio?: number
}

type CleanupReport = {
  found_files?: CleanupFile[]
  found_file_count?: number
  landscape_image_count?: number
  non_portrait_image_count?: number
  large_video_file_count?: number
  bad_video_file_count?: number
  similar_video_duplicate_count?: number
  warnings?: string[]
}

type CleanupProgress = {
  phase?: string
  message?: string
  tool_id?: string
  request_id?: string
  target_dir?: string
  folder_current?: number
  folder_total?: number
  current_folder?: string
  current_file?: string
  source_file_count?: number
  found_file_count?: number
}

type SortPlanAction = {
  source?: string
  destination?: string
  status?: string
  reason?: string
  [key: string]: unknown
}

type SortPlan = {
  ok?: boolean
  plan_id?: string
  id?: string
  action_count?: number
  actions?: SortPlanAction[]
  operations?: SortPlanAction[]
  summary?: {
    ready?: number
    skipped?: number
    unmatched?: number
    unstable?: number
    filtered?: number
  }
  warnings?: string[]
  expires_at?: string
  [key: string]: unknown
}

type HistoryEntry = {
  id: string
  action: string
  timestamp: number
  ok: boolean
  detail: string
}

type SorterProfile = {
  target_dir?: string
  source_dir?: string
  path?: string
  enabled?: boolean
  migration_required_review?: boolean
  migration_rejected_rule_count?: number
  [key: string]: unknown
}

const SORTER_FLAGS = {
  preview: '--preview-json',
  applyPlan: '--apply-plan',
  undoLast: '--undo-last',
  history: '--history-json',
  profiles: '--profiles-json',
  setProfileEnabled: '--set-profile-enabled',
} as const

const READ_ONLY_QUEUE_TTL_MS = 8_000
const SHORT_REQUEST_TIMEOUT_MS = 20_000
const PLAN_JSON_PREFIXES = [
  'FILE_SORTER_PREVIEW_JSON=',
  'FILE_SORTER_PLAN_JSON=',
]
const PROFILES_JSON_PREFIXES = ['FILE_SORTER_PROFILES_JSON=']

function parseKeywords(value: string): string[] {
  return Array.from(
    new Set(
      value
        .split(/[\n,，]+/)
        .map((item) => item.trim())
        .filter(Boolean)
    )
  )
}

export function isDirectChildFolderName(value: string): boolean {
  const folderName = value.trim()
  return (
    folderName.length > 0 &&
    folderName !== '.' &&
    folderName !== '..' &&
    !folderName.includes('/') &&
    !folderName.includes('\\') &&
    !folderName.includes(':')
  )
}

function parseDestinationFolders(stdout: string | undefined): string[] {
  const line = String(stdout || '')
    .split(/\r?\n/)
    .find((item) => item.startsWith(FOLDERS_JSON_PREFIX))
  if (!line) return []

  try {
    const parsed = JSON.parse(line.slice(FOLDERS_JSON_PREFIX.length))
    if (!Array.isArray(parsed)) return []
    return parsed
      .map((item) => String(item).trim())
      .filter(isDirectChildFolderName)
  } catch {
    return []
  }
}

function parseJsonWithPrefixes<T>(
  stdout: string | undefined,
  prefixes: string[]
): T | null {
  const direct = parseToolJson<T>(stdout)
  if (direct) return direct
  const lines = String(stdout || '').split(/\r?\n/)
  for (const prefix of prefixes) {
    const line = lines.find((item) => item.startsWith(prefix))
    if (!line) continue
    try {
      const parsed = JSON.parse(line.slice(prefix.length))
      if (parsed && typeof parsed === 'object') return parsed as T
    } catch {
      // Try other supported output prefixes.
    }
  }
  return null
}

function parseSortPlan(stdout: string | undefined): SortPlan | null {
  const parsed = parseJsonWithPrefixes<SortPlan | { plan?: SortPlan }>(
    stdout,
    PLAN_JSON_PREFIXES
  )
  if (!parsed) return null
  if ('plan' in parsed && parsed.plan && typeof parsed.plan === 'object') {
    return parsed.plan as SortPlan
  }
  return parsed as SortPlan
}

function sortPlanId(plan: SortPlan | null): string {
  return String(plan?.plan_id || plan?.id || '').trim()
}

function sortPlanActionCount(plan: SortPlan | null): number {
  if (!plan) return 0
  if (Array.isArray(plan.operations)) return plan.operations.length
  if (Array.isArray(plan.actions)) return plan.actions.length
  return Number(plan.action_count || plan.summary?.ready || 0)
}

function sortPlanActions(plan: SortPlan | null): SortPlanAction[] {
  if (!plan) return []
  if (Array.isArray(plan.operations)) return plan.operations
  return Array.isArray(plan.actions) ? plan.actions : []
}

function normalizePathForComparison(value: string): string {
  return value.trim().replace(/[\\/]+$/, '').replace(/\//g, '\\').toLowerCase()
}

function parseProfileState(
  stdout: string | undefined,
  targetDir: string
): SorterProfile | null {
  const parsed = parseJsonWithPrefixes<
    SorterProfile[] | SorterProfile | { profiles?: SorterProfile[] }
  >(stdout, PROFILES_JSON_PREFIXES)
  if (!parsed) return null
  const profiles = Array.isArray(parsed)
    ? parsed
    : Array.isArray(parsed.profiles)
      ? parsed.profiles
      : [parsed as SorterProfile]
  const normalizedTarget = normalizePathForComparison(targetDir)
  const profile = profiles.find((item) => {
    const profilePath = String(
      item.target_dir || item.source_dir || item.path || ''
    )
    return normalizePathForComparison(profilePath) === normalizedTarget
  })
  return profile || null
}

function parseCleanupReport(stdout: string | undefined): CleanupReport | null {
  return parseToolJson<CleanupReport>(stdout)
}

function categoryLabel(category: string): string {
  if (category === 'landscape_image') return '橫向圖片'
  if (category === 'non_portrait_image') return '非直式圖片'
  if (category === 'large_video_file') return '過大影片'
  if (category === 'bad_video_file') return '影片問題'
  if (category === 'similar_video_duplicate') return '相似影片'
  return category
}

function cleanupFileSummary(file: CleanupFile): string {
  const parts = [
    (file.categories || []).map(categoryLabel).join(' / '),
    formatFileSize(file.size),
  ].filter(Boolean)

  if (typeof file.video_similarity === 'number') {
    parts.push(`相似度 ${Math.round(file.video_similarity)}%`)
  }
  if (file.similar_to) parts.push(`相似於 ${file.similar_to}`)
  if (file.video_issue) parts.push(`影片狀態 ${file.video_issue}`)
  if (file.width && file.height) parts.push(`${file.width}x${file.height}`)

  return parts.join(' - ') || '已列入清理候選'
}

function progressText(progress: CleanupProgress | null): string {
  if (!progress) return '尚未開始掃描'
  if (progress.message) return progress.message
  if (progress.phase === 'folder_scan') return `掃描資料夾 ${progress.current_folder || ''}`
  if (progress.phase === 'image_analysis') return `分析圖片 ${progress.current_file || ''}`
  if (progress.phase === 'video_analysis') return `分析影片 ${progress.current_file || ''}`
  return progress.phase || '掃描中'
}

export function FileSorterWindowApp() {
  const { cancelToolRun, requestToolRun, socketStatus } = useToolRunner(TOOL_ID, 30 * 60 * 1000)
  const [targetDir, setTargetDir] = useState('')
  const [keywordInput, setKeywordInput] = useState('')
  const [keywordFolder, setKeywordFolder] = useState('')
  const [destinationFolders, setDestinationFolders] = useState<string[]>([])
  const [autoScanFolders, setAutoScanFolders] = useState(true)
  const [folderScanStatus, setFolderScanStatus] = useState('')
  const [autoOrganizeFiles, setAutoOrganizeFiles] = useState(false)
  const [autoOrganizeStatus, setAutoOrganizeStatus] = useState('')
  const [profileRequiresMigrationReview, setProfileRequiresMigrationReview] =
    useState(false)
  const [currentKeyword, setCurrentKeyword] = useState('')
  const [updatedKeyword, setUpdatedKeyword] = useState('')
  const [runState, setRunState] = useState<RunState>('idle')
  const [message, setMessage] = useState('自動化檔案管理已就緒')
  const [output, setOutput] = useState('')
  const [cleanupState, setCleanupState] = useState<RunState>('idle')
  const [cleanupMessage, setCleanupMessage] = useState('清理掃描已併入此工具')
  const [cleanupOutput, setCleanupOutput] = useState('')
  const [cleanupProgress, setCleanupProgress] = useState<CleanupProgress | null>(null)
  const [cleanupFiles, setCleanupFiles] = useState<CleanupFile[]>([])
  const [cleanupImageIssues, setCleanupImageIssues] = useState(false)
  const [cleanupVideoIssues, setCleanupVideoIssues] = useState(true)
  const [cleanupSimilarVideos, setCleanupSimilarVideos] = useState(true)
  const [cleanupParallel, setCleanupParallel] = useState(true)
  const [cleanupThreshold, setCleanupThreshold] = useState(96)
  const [cleanupSpeed, setCleanupSpeed] = useState(50)
  const [cleanupStopRequested, setCleanupStopRequested] = useState(false)
  const [sortPlan, setSortPlan] = useState<SortPlan | null>(null)
  const [planConfirmed, setPlanConfirmed] = useState(false)
  const [historyOpen, setHistoryOpen] = useState(false)
  const [historyEntries, setHistoryEntries] = useState<HistoryEntry[]>([])
  const [historyOutput, setHistoryOutput] = useState('')
  const cleanupRequestIdRef = useRef('')
  const cleanupAbortControllerRef = useRef<AbortController | null>(null)
  const profileSelectionVersionRef = useRef(0)

  const keywords = useMemo(() => parseKeywords(keywordInput), [keywordInput])
  const destinationFolderOptions = useMemo(
    () => Array.from(new Set(destinationFolders)).sort((a, b) => a.localeCompare(b)),
    [destinationFolders]
  )
  const hasSelectedDestinationFolder =
    isDirectChildFolderName(keywordFolder) &&
    destinationFolderOptions.includes(keywordFolder.trim())
  const actionBusy = runState === 'running' || cleanupState === 'running'
  const canRun = !actionBusy && targetDir.trim().length > 0
  const backendConnected = socketStatus === 'Connected'
  const canMutate = canRun && backendConnected
  const canChangeAutoClassification =
    backendConnected && !actionBusy && targetDir.trim().length > 0
  const cleanupEnabled = cleanupImageIssues || cleanupVideoIssues || cleanupSimilarVideos
  const canCleanup = canRun && cleanupEnabled
  const activePlanId = sortPlanId(sortPlan)
  const activePlanActionCount = sortPlanActionCount(sortPlan)
  const activePlanActions = sortPlanActions(sortPlan)
  const folderCurrent = Number(cleanupProgress?.folder_current || 0)
  const folderTotal = Number(cleanupProgress?.folder_total || 0)
  const progressPercent =
    folderTotal > 0
      ? Math.min(100, Math.round((folderCurrent / folderTotal) * 100))
      : cleanupState === 'success'
        ? 100
        : 0

  const updateTargetDir = (value: string) => {
    profileSelectionVersionRef.current += 1
    setTargetDir(value)
    setAutoOrganizeFiles(false)
    setKeywordFolder('')
    setDestinationFolders([])
    setFolderScanStatus('')
    setAutoOrganizeStatus('')
    setProfileRequiresMigrationReview(false)
    setRunState('idle')
    setMessage('自動化檔案管理已就緒')
    setOutput('')
    setCleanupState('idle')
    setCleanupMessage('清理掃描已併入此工具')
    setCleanupProgress(null)
    setCleanupFiles([])
    setCleanupOutput('')
    setSortPlan(null)
    setPlanConfirmed(false)
    setHistoryOpen(false)
    setHistoryEntries([])
    setHistoryOutput('')
  }

  const appendHistory = (action: string, ok: boolean, detail: string) => {
    setHistoryEntries((current) =>
      [
        {
          id: `${Date.now()}:${Math.random().toString(16).slice(2)}`,
          action,
          timestamp: Date.now(),
          ok,
          detail,
        },
        ...current,
      ].slice(0, 20)
    )
  }

  const execute = async (
    args: string[],
    running: string,
    success: string,
    options: ToolRunOptions = {}
  ) => {
    setRunState('running')
    setMessage(running)
    setOutput('')
    try {
      const result = await requestToolRun(args, options)
      const ok = result.ok === true
      setRunState(ok ? 'success' : 'error')
      setMessage(ok ? success : result.message || '工具執行失敗')
      setOutput(formatRunOutput(result))
      return result
    } catch (error) {
      setRunState('error')
      setMessage(error instanceof Error ? error.message : '工具執行時發生錯誤')
      return null
    }
  }

  useEffect(() => {
    const handler = (event: Event) => {
      const detail = (event as CustomEvent).detail || {}
      if (detail.event !== 'toolbox_run_tool_progress') return
      const payload = (detail.payload || {}) as CleanupProgress
      const activeRequestId = cleanupRequestIdRef.current
      if (!activeRequestId) return
      if (payload.tool_id !== TOOL_ID) return
      if (String(payload.request_id || '') !== activeRequestId) return
      if (!payload.phase) return
      setCleanupProgress(payload)
      setCleanupMessage(progressText(payload))
    }
    window.addEventListener('ipc_event', handler)
    return () => window.removeEventListener('ipc_event', handler)
  }, [])

  useEffect(() => {
    const target = targetDir.trim()
    if (!target) return

    let cancelled = false
    const selectionVersion = profileSelectionVersionRef.current
    const abortController = new AbortController()
    const timer = window.setTimeout(() => {
      void requestToolRun([target, SORTER_FLAGS.profiles], {
        mode: 'read-only',
        queueTtlMs: READ_ONLY_QUEUE_TTL_MS,
        timeoutMs: SHORT_REQUEST_TIMEOUT_MS,
        signal: abortController.signal,
      })
        .then((result) => {
          if (
            cancelled ||
            profileSelectionVersionRef.current !== selectionVersion ||
            result.ok !== true
          ) {
            return
          }
          const profile = parseProfileState(result.stdout, target)
          if (!profile || typeof profile.enabled !== 'boolean') return
          const requiresReview = profile.migration_required_review === true
          setProfileRequiresMigrationReview(requiresReview)
          setAutoOrganizeFiles(profile.enabled && !requiresReview)
          if (requiresReview) {
            const rejectedCount = Number(profile.migration_rejected_rule_count || 0)
            setAutoOrganizeStatus(
              `舊規則已完整保留並隔離${
                rejectedCount > 0 ? `（${rejectedCount} 筆需確認）` : ''
              }；確認後才能重新啟用自動分類`
            )
          } else if (!profile.enabled) {
            setAutoOrganizeStatus('此資料夾的自動分類設定為關閉')
          }
        })
        .catch(() => {
          // Profiles are an optional persistence layer; local safe defaults remain active.
        })
    }, 500)

    return () => {
      cancelled = true
      abortController.abort()
      window.clearTimeout(timer)
    }
  }, [requestToolRun, targetDir])

  useEffect(() => {
    const target = targetDir.trim()
    if (!autoScanFolders || !target) {
      setDestinationFolders([])
      setKeywordFolder('')
      setFolderScanStatus('')
      return
    }

    let cancelled = false
    const abortController = new AbortController()
    const timer = window.setTimeout(() => {
      setFolderScanStatus('正在掃描可用目的地資料夾...')
      void requestToolRun([target, '--list-folders'], {
        mode: 'read-only',
        queueTtlMs: READ_ONLY_QUEUE_TTL_MS,
        timeoutMs: SHORT_REQUEST_TIMEOUT_MS,
        signal: abortController.signal,
      })
        .then((result) => {
          if (cancelled) return
          if (result.ok !== true) {
            setDestinationFolders([])
            setKeywordFolder('')
            setFolderScanStatus(result.message || '資料夾掃描失敗')
            return
          }
          const folders = parseDestinationFolders(result.stdout)
          setDestinationFolders(folders)
          setKeywordFolder((current) =>
            folders.includes(current.trim()) ? current.trim() : ''
          )
          setFolderScanStatus(
            folders.length > 0
              ? `已找到 ${folders.length} 個目的地資料夾`
              : '尚未找到可用目的地資料夾'
          )
        })
        .catch((error) => {
          if (cancelled) return
          setDestinationFolders([])
          setKeywordFolder('')
          setFolderScanStatus(error instanceof Error ? error.message : '資料夾掃描失敗')
        })
    }, 600)

    return () => {
      cancelled = true
      abortController.abort()
      window.clearTimeout(timer)
    }
  }, [autoScanFolders, requestToolRun, targetDir])

  useEffect(() => {
    const target = targetDir.trim()
    if (!autoOrganizeFiles || !target) {
      if (!autoOrganizeFiles) {
        setAutoOrganizeStatus(
          target
            ? profileRequiresMigrationReview
              ? '舊規則已完整保留並等待確認；自動分類維持關閉'
              : '此資料夾的自動分類設定為關閉'
            : ''
        )
      }
      return
    }
    setAutoOrganizeStatus(
      backendConnected
        ? '背景自動分類已啟用；即使關閉此工具視窗仍會持續監看'
        : '後端連線已中斷；目前設定僅供檢視，重新連線後才能變更'
    )
  }, [
    autoOrganizeFiles,
    backendConnected,
    profileRequiresMigrationReview,
    targetDir,
  ])

  const chooseTarget = async () => {
    const folder = await selectFolder()
    if (folder) updateTargetDir(folder)
  }

  const changeAutoOrganizeFiles = (enabled: boolean) => {
    const target = targetDir.trim()

    if (!target) {
      setAutoOrganizeFiles(false)
      setAutoOrganizeStatus('請先選擇目標資料夾')
      return
    }
    if (!backendConnected) {
      setAutoOrganizeStatus('後端連線後才能變更自動分類設定')
      return
    }
    const selectionVersion = profileSelectionVersionRef.current + 1
    profileSelectionVersionRef.current = selectionVersion
    if (
      enabled &&
      profileRequiresMigrationReview &&
      !window.confirm(
        '舊版規則中有超出目前資料夾邊界的項目，原始資料已完整保留在隔離紀錄。' +
          '確定要接受安全遷移結果並啟用自動分類嗎？'
      )
    ) {
      setAutoOrganizeStatus('尚未確認舊規則；自動分類維持關閉')
      return
    }

    setAutoOrganizeStatus(
      enabled
        ? '正在啟用背景自動分類...'
        : '正在關閉背景自動分類...'
    )

    void requestToolRun(
      [target, SORTER_FLAGS.setProfileEnabled, String(enabled)],
      {
        mode: 'mutation',
        timeoutMs: SHORT_REQUEST_TIMEOUT_MS,
      }
    )
      .then((result) => {
        if (profileSelectionVersionRef.current !== selectionVersion) return
        if (result.ok === true) {
          setAutoOrganizeFiles(enabled)
          if (enabled) setProfileRequiresMigrationReview(false)
          setAutoOrganizeStatus(
            enabled
              ? '背景自動分類已啟用；即使關閉此工具視窗仍會持續監看'
              : '自動分類已關閉'
          )
          return
        }
        setAutoOrganizeStatus(
          `自動分類設定未變更：${result.message || '後端不支援 profile'}`
        )
      })
      .catch((error) => {
        if (profileSelectionVersionRef.current !== selectionVersion) return
        setAutoOrganizeStatus(
          `自動分類設定未變更：${
            error instanceof Error ? error.message : '未知錯誤'
          }`
        )
      })
  }

  const refreshDestinationFolders = async () => {
    if (!canRun) return
    const result = await execute(
      [targetDir.trim(), '--list-folders'],
      '正在掃描目的地資料夾...',
      '目的地資料夾已更新',
      {
        mode: 'read-only',
        queueTtlMs: READ_ONLY_QUEUE_TTL_MS,
        timeoutMs: SHORT_REQUEST_TIMEOUT_MS,
      }
    )
    if (result?.ok !== true) {
      setDestinationFolders([])
      setKeywordFolder('')
      setFolderScanStatus(result?.message || '資料夾掃描失敗')
      return
    }
    const folders = parseDestinationFolders(result?.stdout)
    setDestinationFolders(folders)
    setKeywordFolder((current) =>
      folders.includes(current.trim()) ? current.trim() : ''
    )
    setFolderScanStatus(
      folders.length > 0 ? `已找到 ${folders.length} 個目的地資料夾` : '尚未找到可用目的地資料夾'
    )
  }

  const previewSorter = () => {
    if (!canRun) return
    setSortPlan(null)
    setPlanConfirmed(false)
    void execute(
      [targetDir.trim(), SORTER_FLAGS.preview],
      '正在建立安全預覽...',
      '預覽計畫已建立',
      {
        mode: 'read-only',
        queueTtlMs: READ_ONLY_QUEUE_TTL_MS,
        timeoutMs: SHORT_REQUEST_TIMEOUT_MS,
      }
    ).then((result) => {
      if (result?.ok !== true) {
        appendHistory('預覽', false, result?.message || '無法建立預覽')
        return
      }
      const plan = parseSortPlan(result.stdout)
      const planId = sortPlanId(plan)
      if (!plan || plan.ok === false || !planId) {
        setRunState('error')
        setMessage('後端未回傳可套用的 plan_id；沒有執行任何搬移')
        appendHistory('預覽', false, '缺少可套用的 plan_id')
        return
      }
      setSortPlan(plan)
      appendHistory(
        '預覽',
        true,
        `${sortPlanActionCount(plan)} 個動作，plan ${planId}`
      )
    })
  }

  const applySortPlan = () => {
    if (!canMutate || !planConfirmed || !activePlanId) return
    const planId = activePlanId
    void execute(
      [targetDir.trim(), SORTER_FLAGS.applyPlan, planId],
      '正在套用已確認的整理計畫...',
      '整理計畫已安全套用',
      { mode: 'mutation' }
    ).then((result) => {
      const ok = result?.ok === true
      appendHistory(
        '套用計畫',
        ok,
        ok ? `plan ${planId}` : result?.message || `plan ${planId} 套用失敗`
      )
      if (!ok) return
      setSortPlan(null)
      setPlanConfirmed(false)
    })
  }

  const undoLastSort = () => {
    if (!canMutate) return
    if (!window.confirm('確定要復原最近一次已完成的檔案整理？')) return
    void execute(
      [targetDir.trim(), SORTER_FLAGS.undoLast],
      '正在復原最近一次整理...',
      '最近一次整理已復原',
      { mode: 'mutation' }
    ).then((result) => {
      const ok = result?.ok === true
      appendHistory('復原', ok, ok ? '最近一次整理' : result?.message || '復原失敗')
    })
  }

  const toggleHistory = () => {
    if (historyOpen) {
      setHistoryOpen(false)
      return
    }
    setHistoryOpen(true)
    if (!canRun) return
    void execute(
      [targetDir.trim(), SORTER_FLAGS.history],
      '正在讀取整理歷史...',
      '整理歷史已更新',
      {
        mode: 'read-only',
        queueTtlMs: READ_ONLY_QUEUE_TTL_MS,
        timeoutMs: SHORT_REQUEST_TIMEOUT_MS,
      }
    ).then((result) => {
      setHistoryOutput(
        result?.ok === true
          ? String(result.stdout || result.message || '')
          : result?.message || '無法讀取後端歷史'
      )
    })
  }

  const listKeywords = () => {
    if (!canRun) return
    void execute(
      [targetDir.trim(), '--list-keywords'],
      '正在讀取關鍵字規則...',
      '關鍵字規則已讀取',
      {
        mode: 'read-only',
        queueTtlMs: READ_ONLY_QUEUE_TTL_MS,
        timeoutMs: SHORT_REQUEST_TIMEOUT_MS,
      }
    )
  }

  const addKeywords = () => {
    if (!canMutate || keywords.length === 0 || !hasSelectedDestinationFolder) return
    const args = [
      targetDir.trim(),
      ...keywords.flatMap((keyword) => ['--upsert-keyword', keyword]),
      '--folder',
      keywordFolder.trim(),
    ]
    void execute(args, '正在新增或更新關鍵字...', '關鍵字規則已更新')
  }

  const updateKeyword = () => {
    if (!canMutate || !currentKeyword.trim() || !updatedKeyword.trim()) return
    if (keywordFolder.trim() && !hasSelectedDestinationFolder) return
    const args = [
      targetDir.trim(),
      '--update-keyword',
      currentKeyword.trim(),
      '--new-keyword',
      updatedKeyword.trim(),
      ...(keywordFolder.trim() ? ['--folder', keywordFolder.trim()] : []),
    ]
    void execute(args, '正在修改關鍵字...', '關鍵字規則已修改')
  }

  const buildCleanupArgs = (): string[] => {
    const args = [targetDir.trim(), '--cleanup-scan', '--json', '--progress-jsonl']
    if (cleanupImageIssues) args.push('--image-cleanup')
    if (cleanupVideoIssues) args.push('--video-cleanup')
    if (cleanupSimilarVideos) args.push('--similar-video-analysis')
    args.push('--similar-video-threshold', String(cleanupThreshold))
    args.push('--analysis-speed', String(cleanupSpeed))
    if (!cleanupParallel) args.push('--no-parallel-analysis')
    return args
  }

  const runCleanup = () => {
    if (!canCleanup) return
    const cleanupAbortController = new AbortController()
    cleanupAbortControllerRef.current = cleanupAbortController
    setCleanupState('running')
    setCleanupMessage('正在執行清理掃描...')
    setCleanupOutput('')
    setCleanupProgress(null)
    setCleanupFiles([])
    setCleanupStopRequested(false)
    void (async () => {
      let requestId = ''
      try {
        const result = await requestToolRun(buildCleanupArgs(), {
          mode: 'read-only',
          queueTtlMs: READ_ONLY_QUEUE_TTL_MS,
          signal: cleanupAbortController.signal,
          onRequestId: (value) => {
            requestId = value
            cleanupRequestIdRef.current = value
          },
        })
        if (result.cancelled) {
          setCleanupState('idle')
          setCleanupMessage('清理掃描已停止')
          return
        }
        if (result.ok !== true) {
          setCleanupState('error')
          setCleanupMessage(result.message || '清理掃描失敗')
          setCleanupOutput(formatRunOutput(result))
          return
        }

        const report = parseCleanupReport(result.stdout)
        const files = Array.isArray(report?.found_files) ? report.found_files : []
        setCleanupFiles(files)
        setCleanupState('success')
        setCleanupMessage(
          files.length > 0
            ? `清理掃描完成，找到 ${files.length} 個候選項目`
            : '清理掃描完成，沒有找到候選項目'
        )
        setCleanupOutput(formatRunOutput(result))
      } catch (error) {
        const errorMessage = error instanceof Error ? error.message : ''
        if (
          cleanupAbortController.signal.aborted ||
          errorMessage.toLowerCase().includes('cancelled')
        ) {
          setCleanupState('idle')
          setCleanupMessage('清理掃描已取消')
        } else {
          setCleanupState('error')
          setCleanupMessage(error instanceof Error ? error.message : '清理掃描發生錯誤')
        }
      } finally {
        if (cleanupRequestIdRef.current === requestId) {
          cleanupRequestIdRef.current = ''
        }
        if (cleanupAbortControllerRef.current === cleanupAbortController) {
          cleanupAbortControllerRef.current = null
        }
        setCleanupStopRequested(false)
      }
    })()
  }

  const stopCleanup = () => {
    if (cleanupState !== 'running' || cleanupStopRequested) return
    const requestId = cleanupRequestIdRef.current
    if (!requestId) {
      cleanupAbortControllerRef.current?.abort(
        new Error('Queued cleanup scan cancelled.')
      )
      setCleanupStopRequested(true)
      setCleanupMessage('已要求取消清理掃描')
      return
    }
    setCleanupStopRequested(true)
    setCleanupMessage('正在停止清理掃描...')
    void cancelToolRun(requestId)
      .then((result) => {
        if (result.ok === true) return
        setCleanupStopRequested(false)
        setCleanupMessage(result.message || '停止清理掃描失敗')
      })
      .catch((error) => {
        setCleanupStopRequested(false)
        setCleanupMessage(error instanceof Error ? error.message : '停止清理掃描失敗')
      })
  }

  const revealFile = async (file: CleanupFile) => {
    const payload = isAbsoluteFilesystemPath(file.path)
      ? { path: file.path, mode: 'reveal' }
      : { basePath: targetDir.trim(), relativePath: file.path, mode: 'reveal' }
    const result = await openPath(payload)
    if (result.ok === false) setCleanupMessage(result.message || '無法顯示檔案位置')
  }

  return (
    <main style={styles.app}>
      <section style={styles.card}>
        <header style={styles.header}>
          <div>
            <div style={styles.kicker}>Standalone Application</div>
            <h1 style={styles.title}>自動化檔案管理</h1>
            <p style={styles.muted}>自動分類、關鍵字規則與清理掃描已整合在同一個工具。</p>
          </div>
          <span style={styles.badge}>{socketStatus}</span>
        </header>

        <div style={styles.fieldGroup}>
          <label style={styles.label}>目標資料夾</label>
          <div style={styles.inlineRow}>
            <input
              value={targetDir}
              onChange={(event) => updateTargetDir(event.target.value)}
              placeholder="選擇或貼上要管理的資料夾"
              disabled={actionBusy}
              style={styles.input}
            />
            <button
              type="button"
              disabled={actionBusy}
              onClick={chooseTarget}
              style={styles.secondaryButton}
            >
              選擇
            </button>
          </div>
          <label style={{ ...styles.checkboxRow, marginTop: 8 }}>
            <input
              type="checkbox"
              checked={autoOrganizeFiles}
              disabled={!canChangeAutoClassification}
              aria-disabled={!canChangeAutoClassification}
              title={
                !backendConnected
                  ? '後端連線後才能變更自動分類設定'
                  : !targetDir.trim()
                    ? '請先選擇目標資料夾'
                    : actionBusy
                      ? '目前有工作正在執行'
                      : undefined
              }
              onChange={(event) => changeAutoOrganizeFiles(event.target.checked)}
            />
            明確啟用自動分類（預設關閉）
          </label>
          {!backendConnected ? (
            <p style={styles.noticeText}>後端連線後才能變更自動分類設定。</p>
          ) : null}
          <p style={styles.noticeText}>
            啟用後由後端常駐服務監看；檔案需連續兩次保持相同大小與修改時間，未完成下載、仍在寫入或被占用時不會觸發分類。
          </p>
          {autoOrganizeStatus ? <p style={styles.noticeText}>{autoOrganizeStatus}</p> : null}
        </div>

        <section style={styles.notice}>
          <strong>安全整理工作流程</strong>
          <p style={styles.noticeText}>
            手動整理只會先建立 dry-run 預覽；必須勾選確認後，才能套用同一個 plan_id。
          </p>
          <div style={styles.actions}>
            <button
              type="button"
              disabled={!canRun}
              onClick={previewSorter}
              style={styles.secondaryButton}
            >
              {runState === 'running' ? '處理中...' : '建立預覽（Dry run）'}
            </button>
            <button
              type="button"
              disabled={!canMutate}
              onClick={undoLastSort}
              style={styles.dangerButton}
            >
              復原上一次
            </button>
            <button
              type="button"
              disabled={actionBusy && !historyOpen}
              onClick={toggleHistory}
              style={styles.secondaryButton}
            >
              {historyOpen ? '關閉歷史' : '整理歷史'}
            </button>
          </div>

          {sortPlan ? (
            <div style={styles.resultPanel}>
              <div style={{ ...styles.statusLine, justifyContent: 'space-between' }}>
                <strong>待套用計畫：{activePlanActionCount} 個動作</strong>
                <span>{activePlanId}</span>
              </div>
              {sortPlan.summary ? (
                <p style={styles.noticeText}>
                  可搬移 {sortPlan.summary.ready ?? activePlanActionCount} · 略過{' '}
                  {sortPlan.summary.skipped ?? 0} · 未匹配{' '}
                  {sortPlan.summary.unmatched ?? 0} · 尚未穩定{' '}
                  {sortPlan.summary.unstable ?? 0} · 已過濾{' '}
                  {sortPlan.summary.filtered ?? 0}
                </p>
              ) : null}
              {Array.isArray(sortPlan.warnings) && sortPlan.warnings.length > 0 ? (
                <p style={{ ...styles.noticeText, color: '#fbbf24' }}>
                  {sortPlan.warnings.join('；')}
                </p>
              ) : null}
              {activePlanActions.length > 0 ? (
                <div style={styles.resultList}>
                  {activePlanActions.slice(0, 50).map((action, index) => (
                    <div
                      key={`${String(action.source || '')}:${String(action.destination || '')}:${index}`}
                      style={styles.resultRow}
                    >
                      <span style={styles.resultText}>
                        <span style={styles.resultPath}>
                          {String(action.source || `動作 ${index + 1}`)}
                        </span>
                        <span style={styles.resultMeta}>
                          {String(action.destination || action.status || action.reason || '')}
                        </span>
                      </span>
                    </div>
                  ))}
                </div>
              ) : null}
              <label style={{ ...styles.checkboxRow, marginTop: 12 }}>
                <input
                  type="checkbox"
                  checked={planConfirmed}
                  disabled={activePlanActionCount === 0}
                  onChange={(event) => setPlanConfirmed(event.target.checked)}
                />
                {activePlanActionCount > 0
                  ? `我已檢查此計畫，確認套用 plan ${activePlanId}`
                  : '此計畫沒有可套用的搬移動作'}
              </label>
              <div style={styles.actions}>
                <button
                  type="button"
                  disabled={
                    !canMutate ||
                    !planConfirmed ||
                    !activePlanId ||
                    activePlanActionCount === 0
                  }
                  onClick={applySortPlan}
                  style={styles.primaryButton}
                >
                  確認並套用
                </button>
              </div>
            </div>
          ) : null}

          {historyOpen ? (
            <div style={styles.resultPanel}>
              <strong>最近操作</strong>
              {historyEntries.length > 0 ? (
                <div style={styles.resultList}>
                  {historyEntries.map((entry) => (
                    <div key={entry.id} style={styles.resultRow}>
                      <span style={styles.resultText}>
                        <span style={styles.resultPath}>
                          {entry.ok ? '成功' : '失敗'} · {entry.action}
                        </span>
                        <span style={styles.resultMeta}>
                          {new Date(entry.timestamp).toLocaleString()} · {entry.detail}
                        </span>
                      </span>
                    </div>
                  ))}
                </div>
              ) : (
                <p style={styles.noticeText}>本次視窗尚無操作記錄。</p>
              )}
              <pre style={{ ...styles.output, marginTop: 12 }}>
                {historyOutput || '後端尚未回傳持久歷史。'}
              </pre>
            </div>
          ) : null}
        </section>

        <section style={styles.notice}>
          <strong>關鍵字分類規則</strong>
          <p style={styles.noticeText}>
            輸入關鍵字，並從目前目標資料夾既有的第一層子資料夾中選擇目的地；分類不會離開此目標。
          </p>
          <div style={{ ...styles.fieldGroup, marginTop: 12 }}>
            <label style={styles.label}>關鍵字</label>
            <textarea
              value={keywordInput}
              onChange={(event) => setKeywordInput(event.target.value)}
              placeholder="idol, live, report"
              style={styles.textarea}
            />
          </div>
          <div style={styles.fieldGroup}>
            <label style={styles.label}>分類目的地（目標內的第一層子資料夾）</label>
            <div style={styles.inlineRow}>
              <select
                aria-label="分類目的地（目標內的第一層子資料夾）"
                value={hasSelectedDestinationFolder ? keywordFolder.trim() : ''}
                onChange={(event) => setKeywordFolder(event.target.value)}
                style={{ ...styles.input, flex: 1 }}
              >
                <option value="">請先掃描並選擇目的地</option>
                {destinationFolderOptions.map((folder) => (
                  <option key={folder} value={folder}>
                    {folder}
                  </option>
                ))}
              </select>
              <button
                type="button"
                disabled={!canRun}
                onClick={refreshDestinationFolders}
                style={styles.secondaryButton}
              >
                掃描
              </button>
            </div>
            <label style={{ ...styles.checkboxRow, marginTop: 8 }}>
              <input
                type="checkbox"
                checked={autoScanFolders}
                onChange={(event) => setAutoScanFolders(event.target.checked)}
              />
              自動掃描目的地資料夾
            </label>
            <p style={styles.noticeText}>
              {folderScanStatus ||
                '只會列出目前目標內既有的第一層子資料夾；不會建立目的地。'}
            </p>
          </div>
          <div style={styles.actions}>
            <button type="button" disabled={actionBusy} onClick={listKeywords} style={styles.secondaryButton}>
              列出規則
            </button>
            <button
              type="button"
              disabled={
                !canMutate || keywords.length === 0 || !hasSelectedDestinationFolder
              }
              onClick={addKeywords}
              style={styles.secondaryButton}
            >
              新增/更新規則
            </button>
          </div>
        </section>

        <section style={styles.notice}>
          <strong>修改既有關鍵字</strong>
          <div style={{ ...styles.inlineRow, marginTop: 12 }}>
            <input
              value={currentKeyword}
              onChange={(event) => setCurrentKeyword(event.target.value)}
              placeholder="目前關鍵字"
              style={styles.input}
            />
            <input
              value={updatedKeyword}
              onChange={(event) => setUpdatedKeyword(event.target.value)}
              placeholder="新關鍵字"
              style={styles.input}
            />
            <button
              type="button"
              disabled={
                !canMutate || !currentKeyword.trim() || !updatedKeyword.trim()
              }
              onClick={updateKeyword}
              style={styles.secondaryButton}
            >
              修改
            </button>
          </div>
        </section>

        <section style={styles.notice}>
          <strong>清理掃描</strong>
          <p style={styles.noticeText}>重複檔與相似圖片功能已移除；目前保留圖片/影片問題掃描與相似影片偵測。</p>
          <div style={{ ...styles.fieldGroup, marginTop: 12 }}>
            <label style={styles.checkboxRow}>
              <input
                type="checkbox"
                checked={cleanupImageIssues}
                onChange={(event) => setCleanupImageIssues(event.target.checked)}
              />
              圖片方向候選
            </label>
            <label style={{ ...styles.checkboxRow, marginTop: 8 }}>
              <input
                type="checkbox"
                checked={cleanupVideoIssues}
                onChange={(event) => setCleanupVideoIssues(event.target.checked)}
              />
              影片問題候選
            </label>
            <label style={{ ...styles.checkboxRow, marginTop: 8 }}>
              <input
                type="checkbox"
                checked={cleanupSimilarVideos}
                onChange={(event) => setCleanupSimilarVideos(event.target.checked)}
              />
              相似影片偵測
            </label>
            <label style={{ ...styles.checkboxRow, marginTop: 8 }}>
              <input
                type="checkbox"
                checked={cleanupParallel}
                onChange={(event) => setCleanupParallel(event.target.checked)}
              />
              平行分析
            </label>
          </div>
          <div style={styles.sliderGrid}>
            <label style={styles.sliderControl}>
              <span style={styles.sliderHeader}>
                <span>相似影片門檻</span>
                <strong>{cleanupThreshold}%</strong>
              </span>
              <input
                type="range"
                min={1}
                max={100}
                value={cleanupThreshold}
                onChange={(event) => setCleanupThreshold(Number(event.target.value))}
                style={styles.rangeInput}
              />
            </label>
            <label style={styles.sliderControl}>
              <span style={styles.sliderHeader}>
                <span>分析速度</span>
                <strong>{cleanupSpeed}%</strong>
              </span>
              <input
                type="range"
                min={1}
                max={100}
                value={cleanupSpeed}
                onChange={(event) => setCleanupSpeed(Number(event.target.value))}
                style={styles.rangeInput}
              />
            </label>
          </div>
          <div style={styles.actions}>
            {cleanupState === 'running' ? (
              <button type="button" disabled={cleanupStopRequested} onClick={stopCleanup} style={styles.dangerButton}>
                {cleanupStopRequested ? '停止中...' : '停止掃描'}
              </button>
            ) : null}
            <button type="button" disabled={!canCleanup} onClick={runCleanup} style={styles.secondaryButton}>
              {cleanupState === 'running' ? '掃描中...' : '執行清理掃描'}
            </button>
          </div>
        </section>

        <section style={styles.resultPanel}>
          <div style={{ ...styles.statusLine, justifyContent: 'space-between' }}>
            <strong>
              清理進度 {folderCurrent}/{folderTotal || '?'}
            </strong>
            <span>{progressPercent}%</span>
          </div>
          <div style={{ height: 8, background: '#1e293b', borderRadius: 999, overflow: 'hidden' }}>
            <div style={{ height: '100%', width: `${progressPercent}%`, background: '#7dd3fc' }} />
          </div>
          <p style={styles.noticeText}>
            {progressText(cleanupProgress)} - 已掃描 {cleanupProgress?.source_file_count ?? 0} 個檔案 / 找到{' '}
            {cleanupProgress?.found_file_count ?? cleanupFiles.length} 個候選
          </p>
        </section>

        {cleanupFiles.length > 0 ? (
          <section style={styles.resultPanel}>
            <div style={{ ...styles.statusLine, justifyContent: 'space-between' }}>
              <strong>清理候選 {cleanupFiles.length} 項</strong>
            </div>
            <div style={styles.resultList}>
              {cleanupFiles.map((file) => (
                <div key={file.path} style={styles.resultRow}>
                  <span style={styles.resultText}>
                    <span style={styles.resultPath}>{file.path}</span>
                    <span style={styles.resultMeta}>{cleanupFileSummary(file)}</span>
                  </span>
                  <button type="button" onClick={() => void revealFile(file)} style={styles.secondaryButton}>
                    顯示
                  </button>
                </div>
              ))}
            </div>
          </section>
        ) : null}

        <section style={styles.statusPanel}>
          <div style={styles.statusLine}>
            <span
              style={{
                ...styles.statusDot,
                background:
                  runState === 'success'
                    ? '#34d399'
                    : runState === 'error'
                      ? '#f87171'
                      : runState === 'running'
                        ? '#fbbf24'
                        : '#64748b',
              }}
            />
            <strong>{message}</strong>
          </div>
          <pre style={styles.output}>{output || '尚未輸出整理結果'}</pre>
        </section>

        <section style={styles.statusPanel}>
          <div style={styles.statusLine}>
            <span
              style={{
                ...styles.statusDot,
                background:
                  cleanupState === 'success'
                    ? '#34d399'
                    : cleanupState === 'error'
                      ? '#f87171'
                      : cleanupState === 'running'
                        ? '#fbbf24'
                        : '#64748b',
              }}
            />
            <strong>{cleanupMessage}</strong>
          </div>
          <pre style={styles.output}>{cleanupOutput || '尚未輸出清理掃描結果'}</pre>
        </section>
      </section>
    </main>
  )
}
