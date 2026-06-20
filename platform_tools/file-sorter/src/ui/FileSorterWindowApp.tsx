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
} from './toolWindowRunner'

type RunState = 'idle' | 'running' | 'success' | 'error'

const TOOL_ID = 'file-sorter'
const FOLDERS_JSON_PREFIX = 'FILE_SORTER_FOLDERS_JSON='
const SOURCE_FILES_JSON_PREFIX = 'FILE_SORTER_SOURCE_FILES_JSON='

type SourceFile = {
  name: string
  size: number
  mtime_ns: number
}

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
  target_dir?: string
  folder_current?: number
  folder_total?: number
  current_folder?: string
  current_file?: string
  source_file_count?: number
  found_file_count?: number
}

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

function parseDestinationFolders(stdout: string | undefined): string[] {
  const line = String(stdout || '')
    .split(/\r?\n/)
    .find((item) => item.startsWith(FOLDERS_JSON_PREFIX))
  if (!line) return []

  try {
    const parsed = JSON.parse(line.slice(FOLDERS_JSON_PREFIX.length))
    if (!Array.isArray(parsed)) return []
    return parsed.map((item) => String(item).trim()).filter(Boolean)
  } catch {
    return []
  }
}

function parseSourceFiles(stdout: string | undefined): SourceFile[] {
  const line = String(stdout || '')
    .split(/\r?\n/)
    .find((item) => item.startsWith(SOURCE_FILES_JSON_PREFIX))
  if (!line) return []

  try {
    const parsed = JSON.parse(line.slice(SOURCE_FILES_JSON_PREFIX.length))
    if (!Array.isArray(parsed)) return []
    return parsed
      .map((item) => ({
        name: String(item?.name || '').trim(),
        size: Number(item?.size || 0),
        mtime_ns: Number(item?.mtime_ns || 0),
      }))
      .filter((item) => item.name)
  } catch {
    return []
  }
}

function sourceFilesFingerprint(files: SourceFile[]): string {
  return files
    .map((file) => `${file.name}:${file.size}:${file.mtime_ns}`)
    .sort()
    .join('|')
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
  const [autoOrganizeFiles, setAutoOrganizeFiles] = useState(true)
  const [autoOrganizeStatus, setAutoOrganizeStatus] = useState('')
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
  const lastAutoOrganizeFingerprintRef = useRef('')

  const keywords = useMemo(() => parseKeywords(keywordInput), [keywordInput])
  const destinationFolderOptions = useMemo(
    () => Array.from(new Set(destinationFolders)).sort((a, b) => a.localeCompare(b)),
    [destinationFolders]
  )
  const actionBusy = runState === 'running' || cleanupState === 'running'
  const canRun = !actionBusy && targetDir.trim().length > 0
  const cleanupEnabled = cleanupImageIssues || cleanupVideoIssues || cleanupSimilarVideos
  const canCleanup = canRun && cleanupEnabled
  const folderCurrent = Number(cleanupProgress?.folder_current || 0)
  const folderTotal = Number(cleanupProgress?.folder_total || 0)
  const progressPercent =
    folderTotal > 0
      ? Math.min(100, Math.round((folderCurrent / folderTotal) * 100))
      : cleanupState === 'success'
        ? 100
        : 0

  const updateTargetDir = (value: string) => {
    setTargetDir(value)
    setDestinationFolders([])
    setFolderScanStatus('')
    setAutoOrganizeStatus('')
    setCleanupProgress(null)
    setCleanupFiles([])
    setCleanupOutput('')
    lastAutoOrganizeFingerprintRef.current = ''
  }

  const execute = async (args: string[], running: string, success: string) => {
    setRunState('running')
    setMessage(running)
    setOutput('')
    try {
      const result = await requestToolRun(args)
      const ok = result.ok !== false
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
      if (payload.tool_id && payload.tool_id !== TOOL_ID) return
      if (!payload.phase) return
      setCleanupProgress(payload)
      setCleanupMessage(progressText(payload))
    }
    window.addEventListener('ipc_event', handler)
    return () => window.removeEventListener('ipc_event', handler)
  }, [])

  useEffect(() => {
    const target = targetDir.trim()
    if (!autoScanFolders || !target) {
      setDestinationFolders([])
      setFolderScanStatus('')
      return
    }

    let cancelled = false
    const timer = window.setTimeout(() => {
      setFolderScanStatus('正在掃描可用目的地資料夾...')
      void requestToolRun([target, '--list-folders'])
        .then((result) => {
          if (cancelled) return
          const folders = parseDestinationFolders(result.stdout)
          setDestinationFolders(folders)
          setFolderScanStatus(
            folders.length > 0
              ? `已找到 ${folders.length} 個目的地資料夾`
              : '尚未找到可用目的地資料夾'
          )
        })
        .catch((error) => {
          if (cancelled) return
          setDestinationFolders([])
          setFolderScanStatus(error instanceof Error ? error.message : '資料夾掃描失敗')
        })
    }, 600)

    return () => {
      cancelled = true
      window.clearTimeout(timer)
    }
  }, [autoScanFolders, requestToolRun, targetDir])

  useEffect(() => {
    const target = targetDir.trim()
    if (!autoOrganizeFiles || !target || cleanupState === 'running') {
      setAutoOrganizeStatus(cleanupState === 'running' ? '清理掃描中，暫停自動分類' : '')
      if (cleanupState !== 'running') lastAutoOrganizeFingerprintRef.current = ''
      return
    }

    let cancelled = false
    let timer: number | undefined

    const scheduleNextScan = () => {
      timer = window.setTimeout(scanAndOrganize, 3000)
    }

    const scanAndOrganize = async () => {
      try {
        setAutoOrganizeStatus('自動偵測新檔案中...')
        const scanResult = await requestToolRun([target, '--list-source-files'])
        if (cancelled) return

        const sourceFiles = parseSourceFiles(scanResult.stdout)
        if (sourceFiles.length === 0) {
          lastAutoOrganizeFingerprintRef.current = ''
          setAutoOrganizeStatus('目前沒有待分類的新檔案')
          scheduleNextScan()
          return
        }

        const fingerprint = sourceFilesFingerprint(sourceFiles)
        if (fingerprint === lastAutoOrganizeFingerprintRef.current) {
          setAutoOrganizeStatus(`${sourceFiles.length} 個檔案待分類，等待變更`)
          scheduleNextScan()
          return
        }

        lastAutoOrganizeFingerprintRef.current = fingerprint
        setAutoOrganizeStatus(`偵測到 ${sourceFiles.length} 個檔案，正在自動分類...`)
        const organizeResult = await requestToolRun([target])
        if (cancelled) return

        setAutoOrganizeStatus(
          organizeResult.ok !== false
            ? `自動分類完成，處理 ${sourceFiles.length} 個檔案`
            : organizeResult.message || '自動分類失敗'
        )
      } catch (error) {
        if (!cancelled) {
          setAutoOrganizeStatus(error instanceof Error ? error.message : '自動分類發生錯誤')
        }
      }

      if (!cancelled) scheduleNextScan()
    }

    timer = window.setTimeout(scanAndOrganize, 1000)

    return () => {
      cancelled = true
      if (timer !== undefined) window.clearTimeout(timer)
    }
  }, [autoOrganizeFiles, cleanupState, requestToolRun, targetDir])

  const chooseTarget = async () => {
    const folder = await selectFolder()
    if (folder) updateTargetDir(folder)
  }

  const chooseKeywordFolder = async () => {
    const folder = await selectFolder()
    if (folder) setKeywordFolder(folder)
  }

  const refreshDestinationFolders = async () => {
    if (!canRun) return
    const result = await execute(
      [targetDir.trim(), '--list-folders'],
      '正在掃描目的地資料夾...',
      '目的地資料夾已更新'
    )
    const folders = parseDestinationFolders(result?.stdout)
    setDestinationFolders(folders)
    setFolderScanStatus(
      folders.length > 0 ? `已找到 ${folders.length} 個目的地資料夾` : '尚未找到可用目的地資料夾'
    )
  }

  const runSorter = () => {
    if (!canRun) return
    void execute([targetDir.trim()], '正在整理檔案...', '檔案整理完成')
  }

  const listKeywords = () => {
    if (!canRun) return
    void execute([targetDir.trim(), '--list-keywords'], '正在讀取關鍵字規則...', '關鍵字規則已讀取')
  }

  const addKeywords = () => {
    if (!canRun || keywords.length === 0 || !keywordFolder.trim()) return
    const args = [
      targetDir.trim(),
      ...keywords.flatMap((keyword) => ['--upsert-keyword', keyword]),
      '--folder',
      keywordFolder.trim(),
    ]
    void execute(args, '正在新增或更新關鍵字...', '關鍵字規則已更新').then((result) => {
      if (result?.ok !== false) lastAutoOrganizeFingerprintRef.current = ''
    })
  }

  const updateKeyword = () => {
    if (!canRun || !currentKeyword.trim() || !updatedKeyword.trim()) return
    const args = [
      targetDir.trim(),
      '--update-keyword',
      currentKeyword.trim(),
      '--new-keyword',
      updatedKeyword.trim(),
      ...(keywordFolder.trim() ? ['--folder', keywordFolder.trim()] : []),
    ]
    void execute(args, '正在修改關鍵字...', '關鍵字規則已修改').then((result) => {
      if (result?.ok !== false) lastAutoOrganizeFingerprintRef.current = ''
    })
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
    setCleanupState('running')
    setCleanupMessage('正在執行清理掃描...')
    setCleanupOutput('')
    setCleanupProgress(null)
    setCleanupFiles([])
    setCleanupStopRequested(false)
    void (async () => {
      try {
        const result = await requestToolRun(buildCleanupArgs())
        const report = parseCleanupReport(result.stdout)
        const files = Array.isArray(report?.found_files) ? report.found_files : []
        setCleanupFiles(files)
        if (result.cancelled) {
          setCleanupState('idle')
          setCleanupMessage('清理掃描已停止')
          return
        }
        const ok = result.ok !== false
        setCleanupState(ok ? 'success' : 'error')
        setCleanupMessage(
          ok
            ? files.length > 0
              ? `清理掃描完成，找到 ${files.length} 個候選項目`
              : '清理掃描完成，沒有找到候選項目'
            : result.message || '清理掃描失敗'
        )
        setCleanupOutput(formatRunOutput(result))
      } catch (error) {
        setCleanupState('error')
        setCleanupMessage(error instanceof Error ? error.message : '清理掃描發生錯誤')
      } finally {
        setCleanupStopRequested(false)
      }
    })()
  }

  const stopCleanup = () => {
    if (cleanupState !== 'running' || cleanupStopRequested) return
    setCleanupStopRequested(true)
    setCleanupMessage('正在停止清理掃描...')
    void cancelToolRun().catch((error) => {
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
              style={styles.input}
            />
            <button type="button" onClick={chooseTarget} style={styles.secondaryButton}>
              選擇
            </button>
          </div>
          <label style={{ ...styles.checkboxRow, marginTop: 8 }}>
            <input
              type="checkbox"
              checked={autoOrganizeFiles}
              onChange={(event) => setAutoOrganizeFiles(event.target.checked)}
            />
            自動偵測新檔案並分類
          </label>
          {autoOrganizeStatus ? <p style={styles.noticeText}>{autoOrganizeStatus}</p> : null}
        </div>

        <section style={styles.notice}>
          <strong>關鍵字分類規則</strong>
          <p style={styles.noticeText}>輸入關鍵字並指定目的地資料夾；既有關鍵字會更新到新的目的地。</p>
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
            <label style={styles.label}>分類目的地</label>
            <div style={styles.inlineRow}>
              <input
                value={keywordFolder}
                onChange={(event) => setKeywordFolder(event.target.value)}
                placeholder="目標資料夾內的子資料夾，或跨磁碟絕對路徑"
                style={styles.input}
              />
              <button type="button" disabled={actionBusy} onClick={chooseKeywordFolder} style={styles.secondaryButton}>
                選擇
              </button>
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
            {destinationFolderOptions.length > 0 ? (
              <select
                value={destinationFolderOptions.includes(keywordFolder) ? keywordFolder : ''}
                onChange={(event) => setKeywordFolder(event.target.value)}
                style={{ ...styles.input, width: '100%', marginTop: 8 }}
              >
                <option value="">從掃描結果選擇目的地</option>
                {destinationFolderOptions.map((folder) => (
                  <option key={folder} value={folder}>
                    {folder}
                  </option>
                ))}
              </select>
            ) : null}
            <p style={styles.noticeText}>{folderScanStatus || '可使用既有資料夾名稱，也可以選擇跨磁碟目的地。'}</p>
          </div>
          <div style={styles.actions}>
            <button type="button" disabled={actionBusy} onClick={listKeywords} style={styles.secondaryButton}>
              列出規則
            </button>
            <button
              type="button"
              disabled={!canRun || keywords.length === 0 || !keywordFolder.trim()}
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
            <button type="button" disabled={actionBusy} onClick={updateKeyword} style={styles.secondaryButton}>
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

        <div style={styles.actions}>
          <button type="button" disabled={!canRun} onClick={runSorter} style={styles.primaryButton}>
            {runState === 'running' ? '整理中...' : '立即整理'}
          </button>
        </div>

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
