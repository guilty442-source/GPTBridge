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
}

interface VaultlyState {
  ok?: boolean
  message?: string
  platforms?: Platform[]
  accounts?: Account[]
  filter_terms?: string[]
  removed_accounts?: RemovedAccount[]
  auto_scan?: Record<string, AutoScanStatus>
  jobs?: Job[]
  destination?: string
  database_path?: string
  workspace_path?: string
  browser_profile_path?: string
  safety_notice?: string
}

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

function statusColor(status: string): string {
  if (status === 'completed') return '#34d399'
  if (status === 'failed' || status === 'cancelled') return '#f87171'
  if (status === 'running') return '#fbbf24'
  return '#7dd3fc'
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

export function VaultlyDownloadCenter({
  sendCommand,
}: VaultlyDownloadCenterProps) {
  const [platforms, setPlatforms] = useState<Platform[]>([])
  const [accounts, setAccounts] = useState<Account[]>([])
  const [filterTerms, setFilterTerms] = useState<string[]>([])
  const [removedAccounts, setRemovedAccounts] = useState<RemovedAccount[]>([])
  const [autoScan, setAutoScan] = useState<Record<string, AutoScanStatus>>({})
  const [accountSearch, setAccountSearch] = useState('')
  const [filterInput, setFilterInput] = useState('')
  const [jobs, setJobs] = useState<Job[]>([])
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set())
  const [multiSelectMode, setMultiSelectMode] = useState(false)
  const [destination, setDestination] = useState('')
  const [conditions, setConditions] = useState<Conditions>(INITIAL_CONDITIONS)
  const [message, setMessage] = useState('載入下載中心中…')
  const [busyAction, setBusyAction] = useState('')
  const [paths, setPaths] = useState({ workspace: '', database: '', browserProfile: '' })
  const [safetyNotice, setSafetyNotice] = useState('')
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
    setPlatforms(Array.isArray(state.platforms) ? state.platforms : [])
    setAccounts(nextAccounts)
    setFilterTerms(Array.isArray(state.filter_terms) ? state.filter_terms : [])
    setRemovedAccounts(
      Array.isArray(state.removed_accounts) ? state.removed_accounts : []
    )
    setAutoScan(state.auto_scan && typeof state.auto_scan === 'object' ? state.auto_scan : {})
    setJobs(Array.isArray(state.jobs) ? state.jobs : [])
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
        const result = (await request('vaultly_get_state')) as VaultlyState
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
    [applyState, request]
  )

  useEffect(() => {
    void loadState()
    const timer = window.setInterval(() => void loadState(true), 4000)
    return () => window.clearInterval(timer)
  }, [loadState])

  const selectedCount = selectedIds.size
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

  const chooseDestination = useCallback(async () => {
    const selected = window.gptBridge?.selectFolder
      ? await window.gptBridge.selectFolder()
      : ((await (window as any).electron?.invoke?.('dialog:select-folder')) as string)
    if (selected) setDestination(selected)
  }, [])

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
    [conditions, destination, runAction, selectedIds]
  )

  return (
    <div style={styles.root}>
      <section style={styles.notice}>
        <strong>正式版專屬瀏覽器工作階段</strong>
        <span>登入一次即可保留工作階段；登入成功後會自動開啟追蹤名單、掃描並套用篩選。</span>
        <span>{safetyNotice}</span>
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
          {jobs.length === 0 ? <span style={styles.emptyText}>尚無工作</span> : jobs.map((job) => (
            <article key={job.job_id} style={styles.jobRow}>
              <span style={{ ...styles.statusDot, background: statusColor(job.status) }} />
              <span style={styles.jobText}>
                <strong>{job.preview_only ? '條件預覽' : '自動下載'} · {job.status}</strong>
                <span>{job.message}</span>
                <span>帳號 {job.progress_current}/{job.progress_total} · 符合 {job.matched} · 成功 {job.downloaded} · 略過 {job.skipped} · 失敗 {job.failed}</span>
              </span>
              {['queued', 'running'].includes(job.status) && <button type="button" style={styles.dangerButton} onClick={() => void runAction(`cancel:${job.job_id}`, 'vaultly_cancel_job', { job_id: job.job_id })}>取消</button>}
            </article>
          ))}
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
  actionRow: { display: 'flex', justifyContent: 'flex-end', gap: 9, marginTop: 14 },
  jobs: { display: 'grid', gap: 8, marginTop: 12 },
  jobRow: { display: 'flex', alignItems: 'center', gap: 10, padding: 10, borderRadius: 9, background: '#111827', border: '1px solid #334155' },
  statusDot: { width: 9, height: 9, borderRadius: '50%', flexShrink: 0 },
  jobText: { display: 'grid', gap: 3, flex: 1, minWidth: 0, color: '#94a3b8', fontSize: 12 },
  footer: { display: 'grid', gap: 4, padding: 12, borderRadius: 10, background: '#132033', color: '#bae6fd', fontSize: 12, overflowWrap: 'anywhere' },
}
