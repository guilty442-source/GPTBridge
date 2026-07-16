import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
} from 'react'

type SendCommand = (command: string, payload?: Record<string, unknown>) => unknown

interface VaultlyDownloadCenterProps {
  sendCommand: SendCommand
}

interface Platform {
  id: string
  name: string
  home_url: string
}

interface Account {
  account_id: string
  platform: string
  handle: string
  display_name: string
  profile_url: string
  avatar_url: string
  verified: boolean
  selected: boolean
}

interface RemovedAccount {
  account_id: string
  platform: string
  handle: string
  display_name: string
  profile_url: string
  avatar_url: string
  verified: boolean
  reason: string
  source: string
  removed_at: string
}

interface AutoScanStatus {
  status: string
  message: string
  last_scan_at: string
}

interface AutomationSummary {
  progress_percent: number
  processed: number
  remaining: number
  elapsed_seconds: number
  throughput_per_minute: number
  success_rate: number
  failure_rate: number
  is_active: boolean
  label: string
}

interface Job {
  job_id: string
  status: string
  preview_only: boolean
  progress_current: number
  progress_total: number
  matched: number
  downloaded: number
  skipped: number
  failed: number
  message: string
  automation_summary?: AutomationSummary
}

interface PostScanJob {
  scan_job_id: string
  status: string
  progress_current: number
  progress_total: number
  discovered: number
  inspected: number
  skipped_existing: number
  failed: number
  message: string
  automation_summary?: AutomationSummary
}

interface DownloadAutomation {
  running_jobs: number
  queued_jobs: number
  running_post_scans: number
  queued_post_scans: number
  queue_depth: number
  has_active_work: boolean
  active_job_id: string
  active_scan_id: string
  total_downloaded: number
  total_failed: number
  success_rate: number
  status_counts: Record<string, number>
  post_scan_status_counts: Record<string, number>
  retry_attempts: number
  max_media_bytes: number
}

interface DestinationHealth {
  ok: boolean
  path: string
  exists: boolean
  is_dir: boolean
  writable: boolean
  free_bytes: number
  message: string
}

interface PostMedia {
  media_id: string
  media_index: number
  media_type: string
  source_url: string
  thumbnail_url: string
  fallback_urls?: string[]
  delivery: string
}

interface VaultlyPost {
  post_id: string
  platform: string
  account_id: string
  account_handle: string
  account_display_name: string
  post_url: string
  text: string
  published_at: string
  likes_text: string
  views_text: string
  media_count: number
  downloadable_count: number
  downloaded_count: number
  thumbnail_url: string
  scan_status: string
  last_error: string
  updated_at: string
  media: PostMedia[]
}

interface VaultlyState {
  ok?: boolean
  version?: string
  message?: string
  platforms?: Platform[]
  accounts?: Account[]
  filter_terms?: string[]
  removed_accounts?: RemovedAccount[]
  auto_scan?: Record<string, AutoScanStatus>
  jobs?: Job[]
  post_scan_jobs?: PostScanJob[]
  download_automation?: DownloadAutomation
  destination_health?: DestinationHealth
  posts?: VaultlyPost[]
  posts_total?: number
  posts_offset?: number
  posts_limit?: number
  destination?: string
  database_path?: string
  workspace_path?: string
  browser_profile_path?: string
  safety_notice?: string
  diagnostics?: VaultlyDiagnostics
}

interface VaultlyFailureItem {
  kind?: string
  id?: string
  status?: string
  failed?: number
  message?: string
  category?: string
}

interface VaultlyFailureSummary {
  total_failures: number
  total_failed_items: number
  categories: Record<string, number>
  latest?: VaultlyFailureItem
  items?: VaultlyFailureItem[]
}

interface VaultlyPlatformDiagnostic {
  id: string
  name: string
  health: string
  status: string
  message: string
  last_scan_at: string
  account_count: number
  selected_count: number
  removed_count: number
}

interface VaultlyDiagnostics {
  state: string
  message: string
  browser?: {
    initialized?: boolean
    requested?: boolean
    user_opened?: boolean
    page_count?: number
    profile_path?: string
  }
  platforms?: VaultlyPlatformDiagnostic[]
  failure_summary?: VaultlyFailureSummary
  generated_at?: string
}

const DEFAULT_PLATFORMS: Platform[] = [
  { id: 'instagram', name: 'Instagram', home_url: 'https://www.instagram.com/' },
  { id: 'x', name: 'X', home_url: 'https://x.com/' },
]

interface Conditions {
  photos: boolean
  videos: boolean
  dateSince: string
  dateUntil: string
  includeKeywords: string
  excludeKeywords: string
  minLikes: number
  minViews: number
  maxItemsPerAccount: number
  skipDownloaded: boolean
}

const INITIAL_CONDITIONS: Conditions = {
  photos: true,
  videos: true,
  dateSince: '',
  dateUntil: '',
  includeKeywords: '',
  excludeKeywords: '',
  minLikes: 0,
  minViews: 0,
  maxItemsPerAccount: 20,
  skipDownloaded: true,
}

const POST_PAGE_SIZE = 40

function waitForIpcEvent(
  eventName: string,
  timeoutMs: number
): Promise<Record<string, unknown>> {
  return new Promise((resolve, reject) => {
    const timer = window.setTimeout(() => {
      window.removeEventListener('ipc_event', handler)
      reject(new Error(`等待 ${eventName} 逾時`))
    }, timeoutMs)
    const handler = (event: Event) => {
      const detail = (event as CustomEvent).detail || {}
      if (detail.event !== eventName) return
      window.clearTimeout(timer)
      window.removeEventListener('ipc_event', handler)
      resolve((detail.payload || {}) as Record<string, unknown>)
    }
    window.addEventListener('ipc_event', handler)
  })
}

function splitKeywords(value: string): string[] {
  return Array.from(
    new Set(
      value
        .split(/[\n,，]+/)
        .map((item) => item.trim())
        .filter(Boolean)
    )
  )
}

function splitLinks(value: string): string[] {
  return Array.from(
    new Set(
      value
        .split(/[\s,]+/)
        .map((item) => item.trim())
        .filter((item) => /^https?:\/\//i.test(item))
    )
  )
}

function statusColor(status: string): string {
  if (status === 'completed') return '#34d399'
  if (status === 'failed' || status === 'cancelled') return '#f87171'
  if (status === 'running') return '#fbbf24'
  return '#7dd3fc'
}

function progressPercent(
  summary: AutomationSummary | undefined,
  current: number,
  total: number
): number {
  if (summary && Number.isFinite(summary.progress_percent)) {
    return Math.max(0, Math.min(100, Math.round(summary.progress_percent)))
  }
  if (!total) return 0
  return Math.max(0, Math.min(100, Math.round((current / total) * 100)))
}

function formatPercent(value: number | undefined): string {
  if (!Number.isFinite(value)) return '0%'
  return `${Math.max(0, Math.min(100, Math.round(value || 0)))}%`
}

function formatRate(value: number | undefined): string {
  if (!value || value <= 0) return '尚未形成速率'
  return `${value.toFixed(value >= 10 ? 0 : 1)} / 分`
}

function formatBytes(value: number | undefined): string {
  const bytes = Number(value || 0)
  if (!bytes || bytes < 0) return '未知'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let current = bytes
  let unitIndex = 0
  while (current >= 1024 && unitIndex < units.length - 1) {
    current /= 1024
    unitIndex += 1
  }
  return `${current.toFixed(unitIndex === 0 ? 0 : 1)} ${units[unitIndex]}`
}

function isDownloadAutomation(value: unknown): value is DownloadAutomation {
  return Boolean(value && typeof value === 'object')
}

function isDestinationHealth(value: unknown): value is DestinationHealth {
  return Boolean(value && typeof value === 'object')
}

function isVaultlyDiagnostics(value: unknown): value is VaultlyDiagnostics {
  return Boolean(value && typeof value === 'object' && 'state' in value)
}

function destinationHealthTone(health: DestinationHealth | null): CSSProperties {
  if (!health) return styles.healthNeutral
  return health.ok ? styles.healthOk : styles.healthBad
}

function diagnosticTone(state?: string): CSSProperties {
  if (state === 'ready') return styles.healthOk
  if (state === 'attention' || state === 'setup') return styles.healthBad
  return styles.healthNeutral
}

function platformHealthTone(health?: string): CSSProperties {
  if (health === 'ready') return styles.healthOk
  if (health === 'attention') return styles.healthBad
  return styles.healthNeutral
}

function diagnosticStateText(state?: string): string {
  if (state === 'ready') return '可用'
  if (state === 'running') return '執行中'
  if (state === 'attention') return '需檢查'
  if (state === 'setup') return '待設定'
  if (state === 'waiting_login') return '等待登入'
  return '待命'
}

function platformHealthText(health?: string): string {
  if (health === 'ready') return '可用'
  if (health === 'running') return '掃描中'
  if (health === 'attention') return '需檢查'
  if (health === 'login_required') return '等待登入'
  return '待命'
}

function failureCategoryText(category?: string): string {
  if (category === 'destination') return '下載位置'
  if (category === 'login') return '登入狀態'
  if (category === 'network') return '網路'
  if (category === 'media') return '媒體解析'
  if (category === 'platform') return '平台版面'
  if (category === 'cancelled') return '已取消'
  return '未分類'
}

function autoScanLabel(status?: AutoScanStatus): string {
  if (!status) return '準備自動掃描'
  if (status.status === 'scanning') return '自動掃描中…'
  if (status.status === 'completed') return '自動掃描已啟用'
  if (status.status === 'error') return '等待自動重試'
  return '等待登入'
}

function matchesAccountSearch(account: Account, query: string): boolean {
  if (!query) return true
  return [
    account.handle,
    account.display_name,
    account.platform,
    account.profile_url,
  ].some((value) => value.toLocaleLowerCase().includes(query))
}

function removedSourceLabel(source: string): string {
  return source === 'manual' ? '手動移除' : '自動篩選'
}

function postStatusLabel(status: string): string {
  if (status === 'ready') return '可瀏覽'
  if (status === 'no_media') return '無媒體'
  if (status === 'error') return '索引失敗'
  return '已發現'
}

function postMediaSummary(post: VaultlyPost): string {
  const photoCount = post.media.filter((media) => media.media_type === 'photo').length
  const videoCount = post.media.filter((media) => media.media_type === 'video').length
  const parts = [
    photoCount ? `${photoCount} 張照片` : '',
    videoCount ? `${videoCount} 支影片` : '',
  ].filter(Boolean)
  return parts.join('、') || `${post.downloadable_count || post.media_count || 0} 個媒體`
}

function formatPostDate(value: string): string {
  if (!value) return '時間未知'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleString('zh-Hant', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  })
}

function AccountAvatar({ account }: { account: Pick<Account, 'avatar_url'> }) {
  const [failed, setFailed] = useState(false)

  useEffect(() => {
    setFailed(false)
  }, [account.avatar_url])

  if (!account.avatar_url || failed) {
    return <span style={styles.avatarFallback}>@</span>
  }

  return (
    <img
      src={account.avatar_url}
      style={styles.avatar}
      alt=""
      referrerPolicy="no-referrer"
      onError={() => setFailed(true)}
    />
  )
}

function PostThumbnail({ post }: { post: VaultlyPost }) {
  const [failed, setFailed] = useState(false)
  const thumbnail = post.thumbnail_url || post.media.find((media) => media.thumbnail_url)?.thumbnail_url || ''

  useEffect(() => {
    setFailed(false)
  }, [thumbnail])

  if (!thumbnail || failed) {
    return (
      <div style={styles.postThumbnailFallback}>
        <span>{post.media.some((media) => media.media_type === 'video') ? 'VIDEO' : 'POST'}</span>
      </div>
    )
  }

  return (
    <img
      src={thumbnail}
      style={styles.postThumbnail}
      alt=""
      referrerPolicy="no-referrer"
      onError={() => setFailed(true)}
    />
  )
}

export function VaultlyDownloadCenter({
  sendCommand,
}: VaultlyDownloadCenterProps) {
  const [platforms, setPlatforms] = useState<Platform[]>(DEFAULT_PLATFORMS)
  const [accounts, setAccounts] = useState<Account[]>([])
  const [filterTerms, setFilterTerms] = useState<string[]>([])
  const [removedAccounts, setRemovedAccounts] = useState<RemovedAccount[]>([])
  const [autoScan, setAutoScan] = useState<Record<string, AutoScanStatus>>({})
  const [accountSearch, setAccountSearch] = useState('')
  const [filterInput, setFilterInput] = useState('')
  const [jobs, setJobs] = useState<Job[]>([])
  const [postScanJobs, setPostScanJobs] = useState<PostScanJob[]>([])
  const [downloadAutomation, setDownloadAutomation] = useState<DownloadAutomation | null>(null)
  const [posts, setPosts] = useState<VaultlyPost[]>([])
  const [postsTotal, setPostsTotal] = useState(0)
  const [postPage, setPostPage] = useState(0)
  const [postSearch, setPostSearch] = useState('')
  const [postPlatformFilter, setPostPlatformFilter] = useState('all')
  const [postStatusFilter, setPostStatusFilter] = useState('all')
  const [selectedPostId, setSelectedPostId] = useState('')
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set())
  const [multiSelectMode, setMultiSelectMode] = useState(false)
  const [destination, setDestination] = useState('')
  const [destinationHealth, setDestinationHealth] = useState<DestinationHealth | null>(null)
  const [conditions, setConditions] = useState<Conditions>(INITIAL_CONDITIONS)
  const [quickLinks, setQuickLinks] = useState('')
  const [message, setMessage] = useState('載入下載中心中…')
  const [version, setVersion] = useState('')
  const [busyAction, setBusyAction] = useState('')
  const [paths, setPaths] = useState({ workspace: '', database: '', browserProfile: '' })
  const [safetyNotice, setSafetyNotice] = useState('')
  const [diagnostics, setDiagnostics] = useState<VaultlyDiagnostics | null>(null)
  const loadingRef = useRef(false)
  const selectionLoadedRef = useRef(false)

  const request = useCallback(
    async (
      command: string,
      payload: Record<string, unknown> = {},
      timeoutMs = 30000
    ) => {
      const resultPromise = waitForIpcEvent(`${command}_result`, timeoutMs)
      sendCommand(command, payload)
      return await resultPromise
    },
    [sendCommand]
  )

  const applyState = useCallback((state: VaultlyState) => {
    const nextAccounts = Array.isArray(state.accounts) ? state.accounts : []
    setPlatforms((current) =>
      Array.isArray(state.platforms) && state.platforms.length > 0
        ? state.platforms
        : current.length > 0
          ? current
          : DEFAULT_PLATFORMS
    )
    setAccounts(nextAccounts)
    setFilterTerms(Array.isArray(state.filter_terms) ? state.filter_terms : [])
    setRemovedAccounts(
      Array.isArray(state.removed_accounts) ? state.removed_accounts : []
    )
    setAutoScan(state.auto_scan && typeof state.auto_scan === 'object' ? state.auto_scan : {})
    setJobs(Array.isArray(state.jobs) ? state.jobs : [])
    setPostScanJobs(
      Array.isArray(state.post_scan_jobs) ? state.post_scan_jobs : []
    )
    setDownloadAutomation(
      isDownloadAutomation(state.download_automation)
        ? state.download_automation
        : null
    )
    setDestinationHealth(
      isDestinationHealth(state.destination_health)
        ? state.destination_health
        : null
    )
    setDiagnostics(isVaultlyDiagnostics(state.diagnostics) ? state.diagnostics : null)
    setVersion(String(state.version || ''))
    const nextPosts = Array.isArray(state.posts) ? state.posts : []
    setPosts(nextPosts)
    setPostsTotal(Number(state.posts_total || nextPosts.length || 0))
    setSelectedPostId((current) => {
      if (current && nextPosts.some((post) => post.post_id === current)) return current
      return nextPosts[0]?.post_id || ''
    })
    setDestination((current) => current || String(state.destination || ''))
    setPaths({
      workspace: String(state.workspace_path || ''),
      database: String(state.database_path || ''),
      browserProfile: String(state.browser_profile_path || ''),
    })
    setSafetyNotice(String(state.safety_notice || ''))
    if (!selectionLoadedRef.current) {
      selectionLoadedRef.current = true
      setSelectedIds(
        new Set(
          nextAccounts
            .filter((account) => account.selected)
            .map((account) => account.account_id)
          )
      )
    } else {
      const knownIds = new Set(nextAccounts.map((account) => account.account_id))
      setSelectedIds((current) =>
        new Set(Array.from(current).filter((accountId) => knownIds.has(accountId)))
      )
    }
  }, [])

  const loadState = useCallback(
    async (silent = false) => {
      if (loadingRef.current) return
      loadingRef.current = true
      try {
        const result = (await request('vaultly_get_state', {
          posts: {
            limit: POST_PAGE_SIZE,
            offset: postPage * POST_PAGE_SIZE,
            platform: postPlatformFilter,
            status: postStatusFilter,
            query: postSearch.trim(),
          },
        })) as VaultlyState
        if (result.ok === false) throw new Error(String(result.message || '載入失敗'))
        applyState(result)
        if (!silent) setMessage('下載中心已就緒')
      } catch (error) {
        if (!silent) {
          setMessage(error instanceof Error ? error.message : '載入下載中心失敗')
        }
      } finally {
        loadingRef.current = false
      }
    },
    [applyState, postPage, postPlatformFilter, postSearch, postStatusFilter, request]
  )

  useEffect(() => {
    void loadState()
    const timer = window.setInterval(() => void loadState(true), 4000)
    return () => window.clearInterval(timer)
  }, [loadState])

  useEffect(() => {
    setPostPage(0)
  }, [postPlatformFilter, postSearch, postStatusFilter])

  const selectedCount = selectedIds.size
  const quickLinkCount = splitLinks(quickLinks).length
  const normalizedAccountSearch = accountSearch.trim().toLocaleLowerCase()
  const groupedAccounts = useMemo(
    () =>
      platforms.map((platform) => {
        const platformAccounts = accounts.filter(
          (account) => account.platform === platform.id
        )
        const visibleAccounts = platformAccounts
          .filter((account) => matchesAccountSearch(account, normalizedAccountSearch))
          .sort((left, right) => {
            const selectedOrder =
              Number(selectedIds.has(right.account_id)) -
              Number(selectedIds.has(left.account_id))
            if (selectedOrder !== 0) return selectedOrder
            return left.handle.localeCompare(right.handle, undefined, {
              sensitivity: 'base',
            })
          })
        return {
          platform,
          accounts: visibleAccounts,
          totalCount: platformAccounts.length,
        }
      }),
    [accounts, normalizedAccountSearch, platforms, selectedIds]
  )
  const visibleAccountCount = groupedAccounts.reduce(
    (total, group) => total + group.accounts.length,
    0
  )
  const visibleAccountIds = useMemo(
    () =>
      groupedAccounts.flatMap((group) =>
        group.accounts.map((account) => account.account_id)
      ),
    [groupedAccounts]
  )
  const visibleSelectedCount = visibleAccountIds.filter((accountId) =>
    selectedIds.has(accountId)
  ).length
  const allVisibleSelected =
    visibleAccountIds.length > 0 &&
    visibleSelectedCount === visibleAccountIds.length
  const automaticRemovedCount = removedAccounts.filter(
    (account) => account.source !== 'manual'
  ).length
  const manualRemovedCount = removedAccounts.length - automaticRemovedCount
  const normalizedPostSearch = postSearch.trim().toLocaleLowerCase()
  const filteredPosts = useMemo(
    () =>
      posts.filter((post) => {
        if (postPlatformFilter !== 'all' && post.platform !== postPlatformFilter) {
          return false
        }
        if (postStatusFilter !== 'all' && post.scan_status !== postStatusFilter) {
          return false
        }
        if (!normalizedPostSearch) return true
        return [
          post.account_handle,
          post.account_display_name,
          post.text,
          post.post_url,
          post.platform,
        ].some((value) => value.toLocaleLowerCase().includes(normalizedPostSearch))
      }),
    [normalizedPostSearch, postPlatformFilter, postStatusFilter, posts]
  )
  const selectedPost =
    posts.find((post) => post.post_id === selectedPostId) || filteredPosts[0] || null
  const readyPostCount = posts.filter((post) => post.scan_status === 'ready').length
  const downloadedPostCount = posts.filter((post) => post.downloaded_count > 0).length
  const activePostScan = postScanJobs.find((job) =>
    ['queued', 'running'].includes(job.status)
  )
  const activeDownloadJob =
    jobs.find((job) => ['queued', 'running'].includes(job.status)) || null
  const automationRunning =
    downloadAutomation?.running_jobs ??
    jobs.filter((job) => job.status === 'running').length
  const automationQueued =
    downloadAutomation?.queued_jobs ??
    jobs.filter((job) => job.status === 'queued').length
  const automationScanning =
    (downloadAutomation?.running_post_scans ?? 0) +
    (downloadAutomation?.queued_post_scans ?? 0)
  const automationDownloaded =
    downloadAutomation?.total_downloaded ??
    jobs.reduce((total, job) => total + Number(job.downloaded || 0), 0)
  const automationFailed =
    downloadAutomation?.total_failed ??
    jobs.reduce((total, job) => total + Number(job.failed || 0), 0)
  const automationSuccessRate = downloadAutomation?.success_rate ?? 0
  const platformDiagnostics = diagnostics?.platforms || []
  const failureSummary = diagnostics?.failure_summary
  const failureCategories = Object.entries(failureSummary?.categories || {})
  const latestFailure = failureSummary?.latest
  const activeDownloadProgress = activeDownloadJob
    ? progressPercent(
        activeDownloadJob.automation_summary,
        activeDownloadJob.progress_current,
        activeDownloadJob.progress_total
      )
    : 0
  const postPageStart = postsTotal === 0 ? 0 : postPage * POST_PAGE_SIZE + 1
  const postPageEnd = Math.min(postsTotal, postPage * POST_PAGE_SIZE + posts.length)
  const canGoPreviousPostPage = postPage > 0
  const canGoNextPostPage = (postPage + 1) * POST_PAGE_SIZE < postsTotal

  const runAction = useCallback(
    async (
      action: string,
      command: string,
      payload: Record<string, unknown>,
      timeoutMs = 30000
    ) => {
      setBusyAction(action)
      try {
        const result = await request(command, payload, timeoutMs)
        if (result.ok === false) {
          throw new Error(String(result.message || '操作失敗'))
        }
        setMessage(String(result.message || '操作完成'))
        await loadState(true)
      } catch (error) {
        setMessage(error instanceof Error ? error.message : '操作失敗')
      } finally {
        setBusyAction('')
      }
    },
    [loadState, request]
  )

  const checkDestination = useCallback(
    async (path: string) => {
      const trimmed = path.trim()
      if (!trimmed) {
        setDestinationHealth(null)
        return
      }
      try {
        const result = await request(
          'vaultly_check_destination',
          { path: trimmed },
          10000
        )
        setDestinationHealth(
          isDestinationHealth(result.destination_health)
            ? result.destination_health
            : null
        )
      } catch {
        setDestinationHealth({
          ok: false,
          path: trimmed,
          exists: false,
          is_dir: false,
          writable: false,
          free_bytes: 0,
          message: '暫時無法檢查下載資料夾',
        })
      }
    },
    [request]
  )

  useEffect(() => {
    const trimmed = destination.trim()
    if (!trimmed) {
      setDestinationHealth(null)
      return
    }
    const timer = window.setTimeout(() => {
      void checkDestination(trimmed)
    }, 500)
    return () => window.clearTimeout(timer)
  }, [checkDestination, destination])

  const chooseDestination = useCallback(async () => {
    const selected = window.gptBridge?.selectFolder
      ? await window.gptBridge.selectFolder()
      : ((await (window as any).electron?.invoke?.('dialog:select-folder')) as string)
    if (selected) {
      setDestination(selected)
      void checkDestination(selected)
    }
  }, [checkDestination])

  const exportReport = useCallback(async () => {
    setBusyAction('diagnostic-report')
    try {
      const result = await request('vaultly_export_report', {}, 10000)
      if (result.ok === false) {
        throw new Error(String(result.message || '診斷報告匯出失敗'))
      }
      const reportPath = String(result.report_path || '')
      setMessage(reportPath ? `診斷報告已匯出：${reportPath}` : '診斷報告已匯出')
      await loadState(true)
    } catch (error) {
      setMessage(error instanceof Error ? error.message : '診斷報告匯出失敗')
    } finally {
      setBusyAction('')
    }
  }, [loadState, request])

  const pasteQuickLinks = useCallback(async () => {
    try {
      const text = await navigator.clipboard?.readText()
      if (!text) {
        setMessage('剪貼簿沒有可貼上的連結。')
        return
      }
      setQuickLinks((current) => [current, text].filter(Boolean).join('\n'))
      setMessage('已貼上剪貼簿連結。')
    } catch {
      setMessage('無法讀取剪貼簿，請手動貼上連結。')
    }
  }, [])

  const createLinkJob = useCallback(
    (previewOnly: boolean) => {
      const links = splitLinks(quickLinks)
      if (!links.length) {
        setMessage('請先貼上 Instagram / X 的貼文、Reel、Story 或 status 連結。')
        return
      }
      if (!previewOnly && !destination.trim()) {
        setMessage('請先選擇下載資料夾。')
        return
      }
      if (!previewOnly && destinationHealth && !destinationHealth.ok) {
        setMessage(destinationHealth.message)
        return
      }
      const mediaTypes = [
        ...(conditions.photos ? ['photo'] : []),
        ...(conditions.videos ? ['video'] : []),
      ]
      if (!mediaTypes.length) {
        setMessage('請至少選擇照片或影片。')
        return
      }
      void runAction('link-job', 'vaultly_create_link_job', {
        links,
        destination: destination.trim(),
        preview_only: previewOnly,
        conditions: {
          media_types: mediaTypes,
          include_keywords: splitKeywords(conditions.includeKeywords),
          exclude_keywords: splitKeywords(conditions.excludeKeywords),
          skip_downloaded: conditions.skipDownloaded,
        },
      })
    },
    [conditions, destination, destinationHealth, quickLinks, runAction]
  )

  const scanSelectedPosts = useCallback(() => {
    if (!selectedIds.size) {
      setMessage('請先勾選要建立貼文庫的帳號。')
      return
    }
    void runAction(
      'post-scan',
      'vaultly_scan_posts',
      {
        account_ids: Array.from(selectedIds),
        limit_per_account: 12,
      },
      180000
    )
  }, [runAction, selectedIds])

  const cancelPostScan = useCallback(
    (scanJobId: string) => {
      void runAction(
        `post-scan-cancel:${scanJobId}`,
        'vaultly_cancel_post_scan',
        { scan_job_id: scanJobId }
      )
    },
    [runAction]
  )

  const retryJob = useCallback(
    (jobId: string) => {
      void runAction(`retry:${jobId}`, 'vaultly_retry_job', { job_id: jobId })
    },
    [runAction]
  )

  const createPostJob = useCallback(
    (post: VaultlyPost, previewOnly: boolean) => {
      if (!previewOnly && !destination.trim()) {
        setMessage('請先選擇下載資料夾。')
        return
      }
      if (!previewOnly && destinationHealth && !destinationHealth.ok) {
        setMessage(destinationHealth.message)
        return
      }
      const mediaTypes = [
        ...(conditions.photos ? ['photo'] : []),
        ...(conditions.videos ? ['video'] : []),
      ]
      if (!mediaTypes.length) {
        setMessage('請至少啟用一種媒體類型。')
        return
      }
      void runAction('post-link-job', 'vaultly_create_link_job', {
        links: [post.post_url],
        destination: destination.trim(),
        preview_only: previewOnly,
        conditions: {
          media_types: mediaTypes,
          include_keywords: splitKeywords(conditions.includeKeywords),
          exclude_keywords: splitKeywords(conditions.excludeKeywords),
          skip_downloaded: conditions.skipDownloaded,
        },
      })
    },
    [conditions, destination, destinationHealth, runAction]
  )

  const toggleAccount = useCallback((accountId: string) => {
    setSelectedIds((current) => {
      const next = new Set(current)
      if (next.has(accountId)) next.delete(accountId)
      else next.add(accountId)
      return next
    })
  }, [])

  const selectVisibleAccounts = useCallback(() => {
    if (!visibleAccountIds.length) {
      setMessage('目前沒有可勾選的帳號')
      return
    }
    setSelectedIds((current) => {
      const next = new Set(current)
      for (const accountId of visibleAccountIds) next.add(accountId)
      return next
    })
  }, [visibleAccountIds])

  const clearVisibleAccounts = useCallback(() => {
    const visibleIds = new Set(visibleAccountIds)
    setSelectedIds((current) =>
      new Set(Array.from(current).filter((accountId) => !visibleIds.has(accountId)))
    )
  }, [visibleAccountIds])

  const clearAllSelectedAccounts = useCallback(() => {
    setSelectedIds(new Set())
  }, [])

  const saveSelection = useCallback(() => {
    void runAction('save', 'vaultly_save_selection', {
      account_ids: Array.from(selectedIds),
    })
  }, [runAction, selectedIds])

  const addFilterTerms = useCallback(() => {
    const terms = splitKeywords(filterInput)
    if (!terms.length) {
      setMessage('請輸入要排除的帳號或關鍵字')
      return
    }
    setFilterInput('')
    void runAction('filter:add', 'vaultly_add_filter_terms', { terms })
  }, [filterInput, runAction])

  const removeFilterTerm = useCallback(
    (term: string) => {
      void runAction(`filter:remove:${term}`, 'vaultly_remove_filter_terms', {
        terms: [term],
      })
    },
    [runAction]
  )

  const removeAccount = useCallback(
    (accountId: string) => {
      void runAction(`account:remove:${accountId}`, 'vaultly_remove_accounts', {
        account_ids: [accountId],
      })
    },
    [runAction]
  )

  const restoreAccount = useCallback(
    (accountId: string) => {
      void runAction(`account:restore:${accountId}`, 'vaultly_restore_accounts', {
        account_ids: [accountId],
      })
    },
    [runAction]
  )

  const createJob = useCallback(
    (previewOnly: boolean) => {
      if (!selectedIds.size) {
        setMessage('請至少勾選一個追蹤帳號')
        return
      }
      if (!previewOnly && !destination.trim()) {
        setMessage('請先選擇既有下載資料夾')
        return
      }
      if (!previewOnly && destinationHealth && !destinationHealth.ok) {
        setMessage(destinationHealth.message)
        return
      }
      const mediaTypes = [
        ...(conditions.photos ? ['photo'] : []),
        ...(conditions.videos ? ['video'] : []),
      ]
      if (!mediaTypes.length) {
        setMessage('照片與影片至少選一種')
        return
      }
      void runAction('job', 'vaultly_create_job', {
        account_ids: Array.from(selectedIds),
        destination: destination.trim(),
        preview_only: previewOnly,
        conditions: {
          media_types: mediaTypes,
          date_since: conditions.dateSince,
          date_until: conditions.dateUntil,
          include_keywords: splitKeywords(conditions.includeKeywords),
          exclude_keywords: splitKeywords(conditions.excludeKeywords),
          min_likes: conditions.minLikes,
          min_views: conditions.minViews,
          max_items_per_account: conditions.maxItemsPerAccount,
          skip_downloaded: conditions.skipDownloaded,
        },
      })
    },
    [conditions, destination, destinationHealth, runAction, selectedIds]
  )

  return (
    <div style={styles.root}>
      <section style={styles.notice}>
        <strong>正式版專屬瀏覽器工作階段</strong>
        <span>登入一次即可保留工作階段；登入成功後會自動開啟追蹤名單、掃描並套用篩選。</span>
        <span>{safetyNotice}</span>
      </section>

      <section style={{ ...styles.section, ...styles.automationSection }}>
        <div style={styles.sectionHeader}>
          <div>
            <h2 style={styles.sectionTitle}>自動化監控</h2>
            <p style={styles.hint}>背景下載、連結快存與貼文索引會自動回報進度與風險。</p>
          </div>
          <span style={styles.counter}>
            v{version || '1.0.0'} · {downloadAutomation?.has_active_work ? '執行中' : '待命'}
          </span>
        </div>
        <div style={styles.automationStats}>
          <span style={styles.automationStat}>
            <strong>{automationRunning}</strong>
            <small>下載中</small>
          </span>
          <span style={styles.automationStat}>
            <strong>{automationQueued}</strong>
            <small>佇列</small>
          </span>
          <span style={styles.automationStat}>
            <strong>{automationScanning}</strong>
            <small>索引工作</small>
          </span>
          <span style={styles.automationStat}>
            <strong>{automationDownloaded}</strong>
            <small>已下載</small>
          </span>
          <span style={styles.automationStat}>
            <strong>{automationFailed}</strong>
            <small>失敗</small>
          </span>
          <span style={styles.automationStat}>
            <strong>{formatPercent(automationSuccessRate)}</strong>
            <small>成功率</small>
          </span>
          <span style={styles.automationStat}>
            <strong>{downloadAutomation?.retry_attempts || 3}</strong>
            <small>自動重試</small>
          </span>
          <span style={styles.automationStat}>
            <strong>{formatBytes(downloadAutomation?.max_media_bytes)}</strong>
            <small>單檔上限</small>
          </span>
        </div>
        <div style={{ ...styles.destinationHealth, ...destinationHealthTone(destinationHealth) }}>
          <strong>{destinationHealth?.ok ? '下載位置可用' : '下載位置需處理'}</strong>
          <span>
            {destinationHealth?.message || '尚未選擇下載資料夾'} · 剩餘空間{' '}
            {formatBytes(destinationHealth?.free_bytes)}
          </span>
        </div>
        <div style={styles.diagnosticsPanel}>
          <div style={styles.diagnosticsHeader}>
            <span style={styles.diagnosticsTitle}>
              <strong>系統診斷</strong>
              <span>{diagnostics?.message || '等待診斷資料'}</span>
            </span>
            <button
              type="button"
              style={styles.secondaryButton}
              disabled={Boolean(busyAction)}
              onClick={() => void exportReport()}
            >
              {busyAction === 'diagnostic-report' ? '匯出中…' : '匯出診斷報告'}
            </button>
          </div>
          <div style={styles.diagnosticsGrid}>
            <span style={{ ...styles.diagnosticStat, ...diagnosticTone(diagnostics?.state) }}>
              <small>整體狀態</small>
              <strong>{diagnosticStateText(diagnostics?.state)}</strong>
            </span>
            <span style={styles.diagnosticStat}>
              <small>登入瀏覽器</small>
              <strong>{diagnostics?.browser?.initialized ? '已啟動' : '待啟動'}</strong>
            </span>
            <span style={styles.diagnosticStat}>
              <small>失敗工作</small>
              <strong>{failureSummary?.total_failures || 0}</strong>
            </span>
            <span style={styles.diagnosticStat}>
              <small>失敗項目</small>
              <strong>{failureSummary?.total_failed_items || 0}</strong>
            </span>
          </div>
          <div style={styles.platformHealthGrid}>
            {platformDiagnostics.length === 0 ? (
              <span style={styles.smallText}>等待平台健康資料</span>
            ) : (
              platformDiagnostics.map((platform) => (
                <span
                  key={platform.id}
                  style={{ ...styles.platformHealthChip, ...platformHealthTone(platform.health) }}
                >
                  <strong>{platform.name}</strong>
                  <span>{platformHealthText(platform.health)} · {platform.account_count} 個帳號</span>
                  <small>{platform.message || '等待掃描狀態'}</small>
                </span>
              ))
            )}
          </div>
          <div style={styles.failureCategoryRow}>
            {failureCategories.length === 0 ? (
              <span style={styles.smallText}>沒有失敗分類</span>
            ) : (
              failureCategories.map(([category, count]) => (
                <span key={category} style={styles.failureCategoryChip}>
                  {failureCategoryText(category)}：{count}
                </span>
              ))
            )}
          </div>
          {latestFailure?.message && (
            <span style={styles.failureNote}>
              最新失敗：{failureCategoryText(latestFailure.category)} · {latestFailure.message}
            </span>
          )}
        </div>
        {activeDownloadJob && (
          <div style={styles.automationProgress}>
            <div style={styles.postScanHeader}>
              <strong>
                {activeDownloadJob.preview_only ? '條件預覽' : '自動下載'} ·{' '}
                {activeDownloadJob.automation_summary?.label || activeDownloadJob.status}
              </strong>
              <span>{formatPercent(activeDownloadProgress)}</span>
            </div>
            <div style={styles.progressTrack}>
              <span
                style={{
                  ...styles.progressBar,
                  width: `${activeDownloadProgress}%`,
                }}
              />
            </div>
            <span style={styles.postMeta}>
              {activeDownloadJob.message} · 帳號 {activeDownloadJob.progress_current}/
              {activeDownloadJob.progress_total} · 成功 {activeDownloadJob.downloaded} ·
              失敗 {activeDownloadJob.failed} · 速率{' '}
              {formatRate(activeDownloadJob.automation_summary?.throughput_per_minute)}
            </span>
          </div>
        )}
      </section>

      <section style={{ ...styles.section, ...styles.quickSaveSection }}>
        <div style={styles.sectionHeader}>
          <div>
            <h2 style={styles.sectionTitle}>連結快存</h2>
            <p style={styles.hint}>
              參考 Video Downloader & Story Saver：複製 Instagram / X 的貼文、Reel、Story 或 status 連結，貼上後可直接預覽或下載。
            </p>
          </div>
          <span style={styles.counter}>已偵測 {quickLinkCount} 個連結</span>
        </div>
        <div style={styles.quickSaveGrid}>
          <label style={{ ...styles.field, ...styles.quickSaveField }}>
            <span>連結清單</span>
            <textarea
              style={{ ...styles.textarea, ...styles.quickLinkInput }}
              value={quickLinks}
              onChange={(event) => setQuickLinks(event.target.value)}
              placeholder={'每行一個連結，例如：\nhttps://www.instagram.com/reel/...\nhttps://x.com/user/status/...'}
            />
          </label>
          <div style={styles.quickSavePanel}>
            <strong>快速操作</strong>
            <span style={styles.smallText}>支援批次連結、略過已下載媒體，下載資料夾沿用下方設定。</span>
            <div style={styles.buttonRow}>
              <button
                type="button"
                style={styles.secondaryButton}
                disabled={Boolean(busyAction)}
                onClick={() => void pasteQuickLinks()}
              >
                貼上剪貼簿
              </button>
              <button
                type="button"
                style={styles.secondaryButton}
                disabled={Boolean(busyAction) || quickLinkCount === 0}
                onClick={() => createLinkJob(true)}
              >
                預覽連結
              </button>
              <button
                type="button"
                style={styles.primaryButton}
                disabled={Boolean(busyAction) || quickLinkCount === 0}
                onClick={() => createLinkJob(false)}
              >
                立即下載
              </button>
            </div>
          </div>
        </div>
      </section>

      <section style={{ ...styles.section, ...styles.postBrowserSection }}>
        <div style={styles.sectionHeader}>
          <div>
            <h2 style={styles.sectionTitle}>貼文瀏覽</h2>
            <p style={styles.hint}>
              在 Vaultly 內建立本機貼文庫；掃描後可直接瀏覽貼文、媒體狀態，並對單篇貼文預覽或下載。
            </p>
          </div>
          <span style={styles.counter}>
            {postsTotal} 篇貼文 · 本頁 {readyPostCount} 篇可瀏覽 · {downloadedPostCount} 篇已下載
          </span>
        </div>
        <div style={styles.postToolbar}>
          <input
            style={styles.postSearchInput}
            value={postSearch}
            onChange={(event) => setPostSearch(event.target.value)}
            placeholder="搜尋帳號、貼文文字或連結"
            aria-label="搜尋貼文"
          />
          <select
            style={styles.selectInput}
            value={postPlatformFilter}
            onChange={(event) => setPostPlatformFilter(event.target.value)}
            aria-label="平台篩選"
          >
            <option value="all">全部平台</option>
            {platforms.map((platform) => (
              <option key={platform.id} value={platform.id}>
                {platform.name}
              </option>
            ))}
          </select>
          <select
            style={styles.selectInput}
            value={postStatusFilter}
            onChange={(event) => setPostStatusFilter(event.target.value)}
            aria-label="狀態篩選"
          >
            <option value="all">全部狀態</option>
            <option value="ready">可瀏覽</option>
            <option value="discovered">已發現</option>
            <option value="no_media">無媒體</option>
            <option value="error">索引失敗</option>
          </select>
          <button
            type="button"
            style={styles.primaryButton}
            disabled={Boolean(busyAction)}
            onClick={scanSelectedPosts}
          >
            {busyAction === 'post-scan' ? '索引中' : '索引已勾選貼文'}
          </button>
        </div>
        {activePostScan && (
          <div style={styles.postScanProgress}>
            <div style={styles.postScanHeader}>
              <strong>
                {activePostScan.status === 'queued' ? '貼文索引等待中' : '貼文索引進行中'}
              </strong>
              <span>
                {activePostScan.progress_current}/{activePostScan.progress_total} 帳號
              </span>
            </div>
            <div style={styles.progressTrack}>
              <span
                style={{
                  ...styles.progressBar,
                  width: `${progressPercent(
                    activePostScan.automation_summary,
                    activePostScan.progress_current,
                    activePostScan.progress_total
                  )}%`,
                }}
              />
            </div>
            <span style={styles.postMeta}>
              {activePostScan.message} · 發現 {activePostScan.discovered} · 檢查{' '}
              {activePostScan.inspected} · 略過既有 {activePostScan.skipped_existing} ·
              失敗 {activePostScan.failed}
            </span>
            <button
              type="button"
              style={styles.dangerButton}
              onClick={() => cancelPostScan(activePostScan.scan_job_id)}
            >
              取消貼文索引
            </button>
          </div>
        )}
        <div style={styles.postBrowserGrid}>
          <div style={styles.postGrid}>
            {filteredPosts.length === 0 ? (
              <span style={styles.emptyText}>
                尚無貼文。先勾選帳號，再按「索引已勾選貼文」建立本機貼文庫。
              </span>
            ) : (
              filteredPosts.map((post) => (
                <article
                  key={post.post_id}
                  style={{
                    ...styles.postCard,
                    ...(selectedPost?.post_id === post.post_id ? styles.postCardSelected : {}),
                  }}
                  onClick={() => setSelectedPostId(post.post_id)}
                >
                  <div style={styles.postThumbFrame}>
                    <PostThumbnail post={post} />
                    {post.media.some((media) => media.media_type === 'video') && (
                      <span style={styles.videoBadge}>影片</span>
                    )}
                  </div>
                  <div style={styles.postCardBody}>
                    <div style={styles.postCardHeader}>
                      <strong>
                        {post.platform} · @{post.account_handle || 'unknown'}
                      </strong>
                      <span style={styles.statusBadge}>
                        {postStatusLabel(post.scan_status)}
                      </span>
                    </div>
                    <p style={styles.postExcerpt}>
                      {post.text || '尚無文字內容'}
                    </p>
                    <span style={styles.postMeta}>
                      {formatPostDate(post.published_at || post.updated_at)} ·{' '}
                      {postMediaSummary(post)}
                    </span>
                    <span style={styles.postMeta}>
                      已下載 {post.downloaded_count} · 可下載 {post.downloadable_count}
                    </span>
                  </div>
                </article>
              ))
            )}
          </div>
          <aside style={styles.postDetailPanel}>
            {selectedPost ? (
              <>
                <div style={styles.postDetailHeader}>
                  <strong>
                    {selectedPost.platform} · @{selectedPost.account_handle || 'unknown'}
                  </strong>
                  <span style={styles.statusBadge}>
                    {postStatusLabel(selectedPost.scan_status)}
                  </span>
                </div>
                <PostThumbnail post={selectedPost} />
                <p style={styles.postDetailText}>
                  {selectedPost.text || '尚無文字內容。'}
                </p>
                <div style={styles.postDetailMeta}>
                  <span>{formatPostDate(selectedPost.published_at || selectedPost.updated_at)}</span>
                  <span>{selectedPost.likes_text || '按讚數未知'}</span>
                  <span>{selectedPost.views_text || '觀看數未知'}</span>
                  <span>{postMediaSummary(selectedPost)}</span>
                  {selectedPost.last_error && <span>{selectedPost.last_error}</span>}
                </div>
                <div style={styles.postMediaList}>
                  {selectedPost.media.length === 0 ? (
                    <span style={styles.emptyText}>尚未索引到可下載媒體</span>
                  ) : (
                    selectedPost.media.map((media) => (
                      <span key={media.media_id} style={styles.mediaChip}>
                        {media.media_type === 'video' ? '影片' : '照片'} #{media.media_index + 1}
                      </span>
                    ))
                  )}
                </div>
                <div style={styles.buttonRow}>
                  <button
                    type="button"
                    style={styles.secondaryButton}
                    disabled={Boolean(busyAction)}
                    onClick={() => createPostJob(selectedPost, true)}
                  >
                    預覽此貼文
                  </button>
                  <button
                    type="button"
                    style={styles.primaryButton}
                    disabled={Boolean(busyAction)}
                    onClick={() => createPostJob(selectedPost, false)}
                  >
                    下載此貼文
                  </button>
                  <button
                    type="button"
                    style={styles.secondaryButton}
                    onClick={() => {
                      setQuickLinks((current) =>
                        [current, selectedPost.post_url].filter(Boolean).join('\n')
                      )
                      setMessage('已加入連結快存。')
                    }}
                  >
                    加入連結快存
                  </button>
                </div>
              </>
            ) : (
              <span style={styles.emptyText}>選擇一篇貼文查看詳情。</span>
            )}
          </aside>
        </div>
        <div style={styles.postPager}>
          <span style={styles.postMeta}>
            顯示 {postPageStart}-{postPageEnd} / {postsTotal}
          </span>
          <div style={styles.buttonRow}>
            <button
              type="button"
              style={styles.secondaryButton}
              disabled={!canGoPreviousPostPage}
              onClick={() => setPostPage((current) => Math.max(0, current - 1))}
            >
              上一頁
            </button>
            <button
              type="button"
              style={styles.secondaryButton}
              disabled={!canGoNextPostPage}
              onClick={() => setPostPage((current) => current + 1)}
            >
              下一頁
            </button>
          </div>
        </div>
      </section>

      <section style={{ ...styles.section, ...styles.scanSection }}>
        <div style={styles.sectionHeader}>
          <div>
            <h2 style={styles.sectionTitle}>1. 登入與掃描追蹤帳號</h2>
            <p style={styles.hint}>只要登入 Instagram 或 X，背景服務就會自動掃描；已認證帳號不會被自動篩選，但仍可手動移除。已勾選帳號會自動置頂。</p>
          </div>
          <span style={styles.counter}>已勾選 {selectedCount}</span>
        </div>
        <div style={styles.searchRow}>
          <label style={styles.searchField}>
            <span style={styles.searchLabel}>帳號搜尋</span>
            <input
              style={styles.searchInput}
              value={accountSearch}
              onChange={(event) => setAccountSearch(event.target.value)}
              placeholder="搜尋帳號、顯示名稱或平台"
              aria-label="搜尋追蹤帳號"
            />
          </label>
          {accountSearch && (
            <button
              type="button"
              style={styles.searchClearButton}
              onClick={() => setAccountSearch('')}
            >
              清除搜尋
            </button>
          )}
          <span style={styles.searchSummary}>
            顯示 {visibleAccountCount}／{accounts.length} 個帳號
          </span>
        </div>
        <div style={styles.selectionToolbar}>
          <button
            type="button"
            style={multiSelectMode ? styles.primaryButton : styles.secondaryButton}
            onClick={() => setMultiSelectMode((current) => !current)}
          >
            {multiSelectMode ? '結束多選模式' : '多選模式'}
          </button>
          <button
            type="button"
            style={styles.secondaryButton}
            disabled={!multiSelectMode || !visibleAccountIds.length || allVisibleSelected}
            onClick={selectVisibleAccounts}
          >
            全選顯示
          </button>
          <button
            type="button"
            style={styles.secondaryButton}
            disabled={!multiSelectMode || visibleSelectedCount === 0}
            onClick={clearVisibleAccounts}
          >
            取消顯示勾選
          </button>
          <button
            type="button"
            style={styles.secondaryButton}
            disabled={!multiSelectMode || selectedCount === 0}
            onClick={clearAllSelectedAccounts}
          >
            清空勾選
          </button>
          <span style={styles.toolbarHint}>
            {multiSelectMode
              ? `多選中：顯示帳號已勾選 ${visibleSelectedCount}／${visibleAccountIds.length}`
              : '開啟多選後，可直接點帳號卡片批次勾選。'}
          </span>
        </div>
        <div style={styles.platformGrid}>
          {groupedAccounts.map(({ platform, accounts: platformAccounts, totalCount }) => (
            <article key={platform.id} style={styles.platformCard}>
              <div style={styles.platformHeader}>
                <strong>{platform.name}</strong>
                <span style={styles.statusBadge}>{autoScanLabel(autoScan[platform.id])}</span>
              </div>
              <div style={styles.platformMeta}>
                <span style={styles.smallText}>
                  顯示 {platformAccounts.length}／{totalCount} 個保留帳號
                </span>
                <span style={styles.smallText}>{autoScan[platform.id]?.message || '等待服務啟動'}</span>
              </div>
              <div style={styles.buttonRow}>
                <button type="button" style={styles.secondaryButton} disabled={Boolean(busyAction)} onClick={() => void runAction(`open:${platform.id}`, 'vaultly_open_platform', { platform: platform.id })}>
                  {busyAction === `open:${platform.id}` ? '開啟中…' : '開啟登入頁'}
                </button>
                <button type="button" style={styles.primaryButton} disabled={Boolean(busyAction)} onClick={() => void runAction(`scan:${platform.id}`, 'vaultly_scan_following', { platform: platform.id }, 120000)}>
                  {busyAction === `scan:${platform.id}` ? '掃描篩選中…' : '立即重新掃描'}
                </button>
              </div>
              <div style={styles.accountList}>
                {platformAccounts.length === 0 ? (
                  <span style={styles.emptyText}>
                    {normalizedAccountSearch ? '沒有符合搜尋的帳號' : '尚未掃描到帳號'}
                  </span>
                ) : (
                  platformAccounts.map((account) => (
                    <div
                      key={account.account_id}
                      style={{
                        ...styles.accountRow,
                        ...(selectedIds.has(account.account_id)
                          ? styles.accountRowSelected
                          : {}),
                        ...(multiSelectMode ? styles.accountRowSelectable : {}),
                      }}
                      onClick={() => {
                        if (multiSelectMode) toggleAccount(account.account_id)
                      }}
                    >
                      <div style={styles.accountSelect}>
                        <input
                          type="checkbox"
                          checked={selectedIds.has(account.account_id)}
                          onClick={(event) => event.stopPropagation()}
                          onChange={() => toggleAccount(account.account_id)}
                        />
                        <AccountAvatar account={account} />
                        <span style={styles.accountText}>
                          <strong>@{account.handle}</strong>
                          <span>{account.display_name || account.profile_url}</span>
                        </span>
                      </div>
                      <span style={styles.accountActions}>
                        {account.verified && (
                          <span style={styles.protectedBadge}>認證不自動篩選</span>
                        )}
                        <button
                          type="button"
                          style={styles.compactDangerButton}
                          disabled={Boolean(busyAction)}
                          onClick={(event) => {
                            event.stopPropagation()
                            removeAccount(account.account_id)
                          }}
                        >
                          移除
                        </button>
                      </span>
                    </div>
                  ))
                )}
              </div>
            </article>
          ))}
        </div>
        <div style={styles.alignEnd}>
          <button type="button" style={styles.secondaryButton} disabled={Boolean(busyAction)} onClick={saveSelection}>儲存勾選帳號</button>
        </div>
      </section>

      <section style={styles.section}>
        <div style={styles.sectionHeader}>
          <div>
            <h2 style={styles.sectionTitle}>2. 自訂篩選與移除紀錄</h2>
            <p style={styles.hint}>篩選名單只會加入你手動輸入的內容；輸入 <strong>@帳號</strong> 可精準排除。手動移除與自動篩選帳號只會保留在移除紀錄。</p>
          </div>
          <span style={styles.counter}>
            自動篩選 {automaticRemovedCount} · 手動移除 {manualRemovedCount}
          </span>
        </div>
        <div style={styles.managementGrid}>
          <div style={styles.managementCard}>
            <strong>篩選名單</strong>
            <div style={styles.filterControls}>
              <textarea
                style={styles.textarea}
                value={filterInput}
                onChange={(event) => setFilterInput(event.target.value)}
                placeholder="@帳號、商店、新聞（逗號或換行分隔）"
              />
              <button type="button" style={styles.primaryButton} disabled={Boolean(busyAction)} onClick={addFilterTerms}>新增篩選</button>
            </div>
            <div style={styles.filterList}>
              {filterTerms.length === 0 ? (
                <span style={styles.emptyText}>尚未加入自訂篩選</span>
              ) : filterTerms.map((term) => (
                <span key={term} style={styles.filterChip}>
                  {term}
                  <button type="button" style={styles.chipButton} disabled={Boolean(busyAction)} onClick={() => removeFilterTerm(term)}>×</button>
                </span>
              ))}
            </div>
          </div>
          <div style={styles.managementCard}>
            <strong>被移除帳號紀錄（包含自動篩選）</strong>
            <div style={styles.removedList}>
              {removedAccounts.length === 0 ? (
                <span style={styles.emptyText}>尚無移除紀錄</span>
              ) : removedAccounts.map((account) => (
                <div key={account.account_id} style={styles.removedRow}>
                  <AccountAvatar account={account} />
                  <span style={styles.removedText}>
                    <span style={styles.removedTitleRow}>
                      <strong>{account.platform} · @{account.handle}</strong>
                      <span
                        style={
                          account.source === 'manual'
                            ? styles.manualSourceBadge
                            : styles.automaticSourceBadge
                        }
                      >
                        {removedSourceLabel(account.source)}
                      </span>
                    </span>
                    <span>{account.reason || '未記錄原因'}</span>
                    <span>{account.removed_at}</span>
                  </span>
                  <button type="button" style={styles.restoreButton} disabled={Boolean(busyAction)} onClick={() => restoreAccount(account.account_id)}>還原</button>
                </div>
              ))}
            </div>
          </div>
        </div>
      </section>

      <section style={styles.section}>
        <h2 style={styles.sectionTitle}>3. 下載條件</h2>
        <div style={styles.formGrid}>
          <label style={styles.field}>
            <span>媒體類型</span>
            <span style={styles.inlineChecks}>
              <label><input type="checkbox" checked={conditions.photos} onChange={(event) => setConditions((current) => ({ ...current, photos: event.target.checked }))} /> 照片</label>
              <label><input type="checkbox" checked={conditions.videos} onChange={(event) => setConditions((current) => ({ ...current, videos: event.target.checked }))} /> 影片</label>
            </span>
          </label>
          <label style={styles.field}><span>每個帳號最多媒體數</span><input style={styles.input} type="number" min={1} max={200} value={conditions.maxItemsPerAccount} onChange={(event) => setConditions((current) => ({ ...current, maxItemsPerAccount: Number(event.target.value) }))} /></label>
          <label style={styles.field}><span>起始日期</span><input style={styles.input} type="date" value={conditions.dateSince} onChange={(event) => setConditions((current) => ({ ...current, dateSince: event.target.value }))} /></label>
          <label style={styles.field}><span>結束日期</span><input style={styles.input} type="date" value={conditions.dateUntil} onChange={(event) => setConditions((current) => ({ ...current, dateUntil: event.target.value }))} /></label>
          <label style={styles.field}><span>至少按讚數</span><input style={styles.input} type="number" min={0} value={conditions.minLikes} onChange={(event) => setConditions((current) => ({ ...current, minLikes: Number(event.target.value) }))} /></label>
          <label style={styles.field}><span>至少觀看數</span><input style={styles.input} type="number" min={0} value={conditions.minViews} onChange={(event) => setConditions((current) => ({ ...current, minViews: Number(event.target.value) }))} /></label>
          <label style={{ ...styles.field, gridColumn: '1 / -1' }}><span>必須包含關鍵字（逗號或換行分隔）</span><textarea style={styles.textarea} value={conditions.includeKeywords} onChange={(event) => setConditions((current) => ({ ...current, includeKeywords: event.target.value }))} /></label>
          <label style={{ ...styles.field, gridColumn: '1 / -1' }}><span>排除關鍵字（逗號或換行分隔）</span><textarea style={styles.textarea} value={conditions.excludeKeywords} onChange={(event) => setConditions((current) => ({ ...current, excludeKeywords: event.target.value }))} /></label>
          <label style={{ ...styles.field, gridColumn: '1 / -1' }}>
            <span>下載資料夾（直接存入，不自動建立子資料夾）</span>
            <div style={styles.pathRow}><input style={styles.input} value={destination} onChange={(event) => setDestination(event.target.value)} placeholder="選擇既有資料夾" /><button type="button" style={styles.secondaryButton} onClick={chooseDestination}>選擇</button></div>
            <span style={{ ...styles.pathHealth, ...destinationHealthTone(destinationHealth) }}>
              {destinationHealth?.message || '尚未檢查下載資料夾'}
            </span>
          </label>
          <label style={{ ...styles.inlineChecks, gridColumn: '1 / -1' }}><input type="checkbox" checked={conditions.skipDownloaded} onChange={(event) => setConditions((current) => ({ ...current, skipDownloaded: event.target.checked }))} />略過已下載媒體</label>
        </div>
        <div style={styles.actionRow}>
          <button type="button" style={styles.secondaryButton} disabled={Boolean(busyAction)} onClick={() => createJob(true)}>先預覽符合項目</button>
          <button type="button" style={styles.primaryButton} disabled={Boolean(busyAction)} onClick={() => createJob(false)}>開始背景自動下載</button>
        </div>
      </section>

      <section style={styles.section}>
        <h2 style={styles.sectionTitle}>4. 下載佇列</h2>
        <div style={styles.jobs}>
          {jobs.length === 0 ? (
            <span style={styles.emptyText}>尚無工作</span>
          ) : (
            jobs.map((job) => {
              const jobProgress = progressPercent(
                job.automation_summary,
                job.progress_current,
                job.progress_total
              )
              return (
                <article key={job.job_id} style={styles.jobRow}>
                  <span style={{ ...styles.statusDot, background: statusColor(job.status) }} />
                  <span style={styles.jobText}>
                    <strong>
                      {job.preview_only ? '條件預覽' : '自動下載'} ·{' '}
                      {job.automation_summary?.label || job.status}
                    </strong>
                    <span>{job.message}</span>
                    <div style={styles.progressTrack}>
                      <span
                        style={{
                          ...styles.progressBar,
                          width: `${jobProgress}%`,
                        }}
                      />
                    </div>
                    <span>
                      進度 {formatPercent(jobProgress)} · 帳號 {job.progress_current}/
                      {job.progress_total} · 符合 {job.matched} · 成功 {job.downloaded} ·
                      略過 {job.skipped} · 失敗 {job.failed}
                    </span>
                    <span style={styles.postMeta}>
                      速率 {formatRate(job.automation_summary?.throughput_per_minute)}
                      {job.automation_summary
                        ? ` · 剩餘 ${job.automation_summary.remaining}`
                        : ''}
                    </span>
                  </span>
                  {['queued', 'running'].includes(job.status) && (
                    <button
                      type="button"
                      style={styles.dangerButton}
                      onClick={() =>
                        void runAction(`cancel:${job.job_id}`, 'vaultly_cancel_job', {
                          job_id: job.job_id,
                        })
                      }
                    >
                      取消
                    </button>
                  )}
                  {['completed', 'failed', 'cancelled'].includes(job.status) && (
                    <button
                      type="button"
                      style={styles.secondaryButton}
                      disabled={Boolean(busyAction)}
                      onClick={() => retryJob(job.job_id)}
                    >
                      重跑
                    </button>
                  )}
                </article>
              )
            })
          )}
        </div>
      </section>

      <section style={styles.footer}>
        <strong>{message}</strong>
        <span>模組：{paths.workspace || '載入中'}</span>
        <span>資料庫：{paths.database || '載入中'}</span>
        <span>登入資料：{paths.browserProfile || '載入中'}</span>
      </section>
    </div>
  )
}

const styles: Record<string, CSSProperties> = {
  root: { display: 'grid', gap: 16 },
  notice: { display: 'grid', gap: 5, padding: 13, borderRadius: 12, background: '#082f49', border: '1px solid #0ea5e9', color: '#e0f2fe', fontSize: 13, lineHeight: 1.55 },
  section: { padding: 15, borderRadius: 12, background: '#0b1220', border: '1px solid #243044' },
  automationSection: { background: '#0d1522', borderColor: '#2f3f56' },
  automationStats: { display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(110px, 1fr))', gap: 8, marginBottom: 10 },
  automationStat: { display: 'grid', gap: 3, minWidth: 0, padding: '8px 0', color: '#cbd5e1' },
  automationProgress: { display: 'grid', gap: 8, padding: 10, borderRadius: 9, background: '#111827', border: '1px solid #334155' },
  destinationHealth: { display: 'grid', gap: 3, marginBottom: 10, padding: 10, borderRadius: 9, fontSize: 12, lineHeight: 1.45 },
  diagnosticsPanel: { display: 'grid', gap: 10, marginBottom: 10, padding: 12, borderRadius: 9, border: '1px solid #334155', background: '#101827', color: '#e2e8f0' },
  diagnosticsHeader: { display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 10, flexWrap: 'wrap' },
  diagnosticsTitle: { display: 'grid', gap: 3, minWidth: 0, color: '#cbd5e1', fontSize: 12 },
  diagnosticsGrid: { display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(120px, 1fr))', gap: 8 },
  diagnosticStat: { display: 'grid', gap: 3, minWidth: 0, padding: 9, borderRadius: 8, border: '1px solid #334155', background: '#111827', color: '#cbd5e1', fontSize: 12 },
  platformHealthGrid: { display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(190px, 1fr))', gap: 8 },
  platformHealthChip: { display: 'grid', gap: 3, minWidth: 0, padding: 9, borderRadius: 8, fontSize: 12, lineHeight: 1.45 },
  failureCategoryRow: { display: 'flex', flexWrap: 'wrap', gap: 6 },
  failureCategoryChip: { padding: '5px 7px', borderRadius: 999, background: '#1e293b', border: '1px solid #475569', color: '#e2e8f0', fontSize: 11, fontWeight: 800 },
  failureNote: { padding: 9, borderRadius: 8, background: '#2b1a13', border: '1px solid #f97316', color: '#fed7aa', fontSize: 12, lineHeight: 1.45, overflowWrap: 'anywhere' },
  healthNeutral: { background: '#111827', border: '1px solid #334155', color: '#cbd5e1' },
  healthOk: { background: '#052e2b', border: '1px solid #10b981', color: '#d1fae5' },
  healthBad: { background: '#3f1111', border: '1px solid #ef4444', color: '#fecaca' },
  quickSaveSection: { background: '#0a1322', borderColor: '#1d4ed8' },
  quickSaveGrid: { display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(240px, 1fr))', gap: 12 },
  quickSaveField: { minWidth: 0 },
  quickSavePanel: { display: 'grid', alignContent: 'start', gap: 10, padding: 12, borderRadius: 10, border: '1px solid #334155', background: '#111827', color: '#f8fafc' },
  quickLinkInput: { minHeight: 118 },
  postBrowserSection: { background: '#09111f', borderColor: '#2563eb' },
  postToolbar: { display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap', marginBottom: 12 },
  postScanProgress: { display: 'grid', gap: 8, padding: 10, borderRadius: 10, border: '1px solid #1d4ed8', background: '#0b1220', marginBottom: 12 },
  postScanHeader: { display: 'flex', justifyContent: 'space-between', gap: 10, color: '#e2e8f0', fontSize: 12 },
  progressTrack: { height: 7, borderRadius: 999, background: '#1e293b', overflow: 'hidden' },
  progressBar: { display: 'block', height: '100%', borderRadius: 999, background: '#38bdf8' },
  postSearchInput: { flex: '1 1 280px', minWidth: 0, padding: '10px 11px', borderRadius: 9, border: '1px solid #334155', background: '#08101d', color: '#f8fafc' },
  selectInput: { minWidth: 130, padding: '10px 11px', borderRadius: 9, border: '1px solid #334155', background: '#08101d', color: '#f8fafc' },
  postBrowserGrid: { display: 'grid', gridTemplateColumns: 'minmax(0, 1fr) minmax(280px, 360px)', gap: 12, alignItems: 'start' },
  postGrid: { display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(230px, 1fr))', gap: 10, maxHeight: 620, overflow: 'auto', paddingRight: 3 },
  postCard: { display: 'grid', gap: 9, padding: 10, borderRadius: 10, border: '1px solid #243044', background: '#111827', cursor: 'pointer' },
  postCardSelected: { borderColor: '#38bdf8', boxShadow: '0 0 0 1px rgba(56, 189, 248, 0.55)' },
  postThumbFrame: { position: 'relative', aspectRatio: '16 / 10', borderRadius: 8, overflow: 'hidden', background: '#020617', border: '1px solid #1e293b' },
  postThumbnail: { width: '100%', height: '100%', objectFit: 'cover', display: 'block', borderRadius: 8, background: '#020617' },
  postThumbnailFallback: { display: 'grid', placeItems: 'center', width: '100%', height: '100%', minHeight: 150, borderRadius: 8, background: '#0f172a', color: '#7dd3fc', fontSize: 12, fontWeight: 900, letterSpacing: '0.08em' },
  videoBadge: { position: 'absolute', right: 7, bottom: 7, padding: '3px 7px', borderRadius: 999, background: 'rgba(2, 6, 23, 0.82)', color: '#f8fafc', fontSize: 11, fontWeight: 900 },
  postCardBody: { display: 'grid', gap: 6, minWidth: 0 },
  postCardHeader: { display: 'flex', justifyContent: 'space-between', gap: 8, alignItems: 'center', color: '#f8fafc', fontSize: 12 },
  postExcerpt: { margin: 0, color: '#cbd5e1', fontSize: 12, lineHeight: 1.45, minHeight: 34, maxHeight: 52, overflow: 'hidden' },
  postMeta: { color: '#94a3b8', fontSize: 11, lineHeight: 1.45 },
  postDetailPanel: { display: 'grid', gap: 10, padding: 12, borderRadius: 10, border: '1px solid #334155', background: '#111827', color: '#f8fafc', position: 'sticky', top: 12 },
  postDetailHeader: { display: 'flex', justifyContent: 'space-between', gap: 8, alignItems: 'center' },
  postDetailText: { margin: 0, color: '#cbd5e1', fontSize: 13, lineHeight: 1.55, maxHeight: 180, overflow: 'auto', whiteSpace: 'pre-wrap' },
  postDetailMeta: { display: 'grid', gap: 4, color: '#94a3b8', fontSize: 12, overflowWrap: 'anywhere' },
  postMediaList: { display: 'flex', flexWrap: 'wrap', gap: 6 },
  mediaChip: { padding: '5px 7px', borderRadius: 999, background: '#1e293b', border: '1px solid #475569', color: '#e2e8f0', fontSize: 11, fontWeight: 800 },
  postPager: { display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 10, flexWrap: 'wrap', marginTop: 12 },
  scanSection: { padding: 19 },
  sectionHeader: { display: 'flex', justifyContent: 'space-between', gap: 12, marginBottom: 12 },
  sectionTitle: { margin: 0, fontSize: 17, color: '#f8fafc' },
  hint: { margin: '5px 0 0', color: '#94a3b8', fontSize: 12 },
  counter: { color: '#7dd3fc', fontSize: 12, fontWeight: 800 },
  searchRow: { display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap', marginBottom: 13 },
  searchField: { display: 'grid', gap: 5, flex: '1 1 360px', minWidth: 0, color: '#cbd5e1', fontSize: 12, fontWeight: 800 },
  searchLabel: { color: '#7dd3fc', fontSize: 11, fontWeight: 900 },
  searchInput: { minWidth: 0, padding: '11px 12px', borderRadius: 9, border: '1px solid #475569', background: '#08101d', color: '#f8fafc', fontSize: 13 },
  searchClearButton: { padding: '8px 10px', borderRadius: 8, border: '1px solid #475569', background: '#1e293b', color: '#e2e8f0', fontWeight: 800, cursor: 'pointer' },
  searchSummary: { marginLeft: 'auto', color: '#94a3b8', fontSize: 12, fontWeight: 700 },
  selectionToolbar: { display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap', margin: '0 0 13px' },
  toolbarHint: { color: '#94a3b8', fontSize: 12, fontWeight: 700 },
  platformGrid: { display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(390px, 1fr))', gap: 14 },
  platformCard: { padding: 14, borderRadius: 10, border: '1px solid #334155', background: '#111827' },
  platformHeader: { display: 'flex', justifyContent: 'space-between', gap: 8, marginBottom: 10 },
  platformMeta: { display: 'grid', gap: 3, marginBottom: 10 },
  smallText: { color: '#94a3b8', fontSize: 12 },
  statusBadge: { padding: '3px 7px', borderRadius: 999, background: '#0c4a6e', color: '#bae6fd', fontSize: 11, fontWeight: 800 },
  buttonRow: { display: 'flex', gap: 8, flexWrap: 'wrap' },
  primaryButton: { padding: '9px 13px', borderRadius: 9, border: '1px solid #7dd3fc', background: '#38bdf8', color: '#082f49', fontWeight: 900, cursor: 'pointer' },
  secondaryButton: { padding: '9px 13px', borderRadius: 9, border: '1px solid #475569', background: '#1e293b', color: '#f8fafc', fontWeight: 800, cursor: 'pointer' },
  dangerButton: { padding: '7px 10px', borderRadius: 8, border: '1px solid #ef4444', background: '#450a0a', color: '#fecaca', fontWeight: 800, cursor: 'pointer' },
  compactDangerButton: { padding: '5px 8px', borderRadius: 7, border: '1px solid #7f1d1d', background: '#450a0a', color: '#fecaca', fontSize: 11, fontWeight: 800, cursor: 'pointer' },
  restoreButton: { padding: '6px 9px', borderRadius: 7, border: '1px solid #10b981', background: '#064e3b', color: '#d1fae5', fontSize: 11, fontWeight: 800, cursor: 'pointer' },
  accountList: { display: 'grid', alignContent: 'start', gap: 6, minHeight: 280, maxHeight: 460, overflow: 'auto', marginTop: 10 },
  accountRow: { display: 'flex', alignItems: 'center', gap: 8, padding: 7, borderRadius: 8, background: '#0b1220' },
  accountRowSelectable: { cursor: 'pointer' },
  accountRowSelected: { background: '#132033', outline: '1px solid #38bdf8' },
  accountSelect: { display: 'flex', alignItems: 'center', gap: 8, flex: 1, minWidth: 0 },
  accountActions: { display: 'inline-flex', alignItems: 'center', gap: 6, flexShrink: 0 },
  protectedBadge: { padding: '3px 6px', borderRadius: 999, background: '#312e81', color: '#c7d2fe', fontSize: 10, fontWeight: 800, whiteSpace: 'nowrap' },
  avatar: { width: 30, height: 30, objectFit: 'cover', borderRadius: '50%' },
  avatarFallback: { display: 'grid', placeItems: 'center', width: 30, height: 30, borderRadius: '50%', background: '#334155', color: '#bae6fd' },
  accountText: { display: 'grid', minWidth: 0, fontSize: 12, color: '#94a3b8' },
  emptyText: { padding: 12, color: '#64748b', fontSize: 13 },
  alignEnd: { display: 'flex', justifyContent: 'flex-end', marginTop: 12 },
  managementGrid: { display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(320px, 1fr))', gap: 12 },
  managementCard: { display: 'grid', alignContent: 'start', gap: 10, padding: 12, borderRadius: 10, border: '1px solid #334155', background: '#111827', color: '#f8fafc' },
  filterControls: { display: 'grid', gap: 8 },
  filterList: { display: 'flex', flexWrap: 'wrap', gap: 6, maxHeight: 180, overflow: 'auto' },
  filterChip: { display: 'inline-flex', alignItems: 'center', gap: 5, padding: '5px 7px', borderRadius: 999, background: '#1e293b', border: '1px solid #475569', color: '#e2e8f0', fontSize: 12 },
  chipButton: { border: 0, background: 'transparent', color: '#fca5a5', cursor: 'pointer', fontSize: 16, lineHeight: 1 },
  removedList: { display: 'grid', gap: 6, maxHeight: 300, overflow: 'auto' },
  removedRow: { display: 'flex', alignItems: 'center', gap: 8, padding: 8, borderRadius: 8, background: '#0b1220' },
  removedText: { display: 'grid', gap: 2, flex: 1, minWidth: 0, color: '#94a3b8', fontSize: 11, overflowWrap: 'anywhere' },
  removedTitleRow: { display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' },
  automaticSourceBadge: { padding: '2px 6px', borderRadius: 999, background: '#78350f', color: '#fde68a', fontSize: 10, fontWeight: 900 },
  manualSourceBadge: { padding: '2px 6px', borderRadius: 999, background: '#3f1d5c', color: '#e9d5ff', fontSize: 10, fontWeight: 900 },
  formGrid: { display: 'grid', gridTemplateColumns: 'repeat(2, minmax(0, 1fr))', gap: 12, marginTop: 13 },
  field: { display: 'grid', gap: 6, color: '#cbd5e1', fontSize: 12, fontWeight: 700 },
  input: { minWidth: 0, width: '100%', boxSizing: 'border-box', padding: '10px 11px', borderRadius: 9, border: '1px solid #334155', background: '#08101d', color: '#f8fafc' },
  textarea: { minHeight: 62, padding: '10px 11px', borderRadius: 9, border: '1px solid #334155', background: '#08101d', color: '#f8fafc', resize: 'vertical' },
  inlineChecks: { display: 'flex', alignItems: 'center', gap: 15, color: '#cbd5e1', fontSize: 13 },
  pathRow: { display: 'flex', gap: 8 },
  pathHealth: { padding: '7px 9px', borderRadius: 8, fontSize: 12, fontWeight: 800 },
  actionRow: { display: 'flex', justifyContent: 'flex-end', gap: 9, marginTop: 14 },
  jobs: { display: 'grid', gap: 8, marginTop: 12 },
  jobRow: { display: 'flex', alignItems: 'center', gap: 10, padding: 10, borderRadius: 9, background: '#111827', border: '1px solid #334155' },
  statusDot: { width: 9, height: 9, borderRadius: '50%', flexShrink: 0 },
  jobText: { display: 'grid', gap: 3, flex: 1, minWidth: 0, color: '#94a3b8', fontSize: 12 },
  footer: { display: 'grid', gap: 4, padding: 12, borderRadius: 10, background: '#132033', color: '#bae6fd', fontSize: 12, overflowWrap: 'anywhere' },
}
