import { useEffect, useMemo, useState } from 'react'
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

interface DuplicateCleanerFoundFile {
  path: string
  categories?: string[]
  size?: number | null
  deleted_during_scan?: boolean
  kept_path?: string | null
  has_face?: boolean | null
  has_person?: boolean | null
  ai_landscape_detected?: boolean
  ai_landscape_score?: number | null
  similar_to?: string
  image_similarity?: number
  video_similarity?: number
  video_issue?: string
  thumbnail?: string | null
}

interface DuplicateCleanerReport {
  found_files?: DuplicateCleanerFoundFile[]
  moved_files?: Array<{ source?: string; destination?: string }>
  deleted_files?: string[]
  deleted_file_count?: number
  skipped_file_count?: number
}

interface DuplicateCleanerProgress {
  phase?: string
  message?: string
  tool_id?: string
  folder_current?: number
  folder_total?: number
  current_folder?: string
  current_file?: string
  source_file_count?: number
  found_file_count?: number
  found_file?: DuplicateCleanerFoundFile
}

const TOOL_ID = 'tool-mpz30cfk-hfnf'

function parseReport(stdout: string | undefined): DuplicateCleanerReport | null {
  return parseToolJson<DuplicateCleanerReport>(stdout)
}

function foundFilesFromReport(report: DuplicateCleanerReport | null): DuplicateCleanerFoundFile[] {
  const moved = new Map(
    (report?.moved_files || [])
      .filter((file) => file.source && file.destination)
      .map((file) => [String(file.source), String(file.destination)])
  )
  return Array.isArray(report?.found_files)
    ? report.found_files
        .filter((file) => file.path)
        .map((file) => ({
          ...file,
          path: moved.get(file.path) || file.path,
        }))
    : []
}

function isExactDuplicate(file: DuplicateCleanerFoundFile): boolean {
  return Boolean(file.categories?.includes('exact_duplicate') && !file.deleted_during_scan)
}

function exactDuplicatePaths(files: DuplicateCleanerFoundFile[]): string[] {
  return files.filter(isExactDuplicate).map((file) => file.path)
}

function mergeFoundFiles(
  current: DuplicateCleanerFoundFile[],
  incoming: DuplicateCleanerFoundFile
): DuplicateCleanerFoundFile[] {
  if (!incoming.path) return current
  const byPath = new Map(current.map((file) => [file.path, { ...file }]))
  const existing = byPath.get(incoming.path)
  byPath.set(incoming.path, {
    ...(existing || {}),
    ...incoming,
    categories: Array.from(new Set([...(existing?.categories || []), ...(incoming.categories || [])])),
  })
  return Array.from(byPath.values()).sort((left, right) => left.path.localeCompare(right.path))
}

function categoryLabel(category: string): string {
  if (category === 'exact_duplicate') return '完全相同重複檔'
  if (category === 'similar_image_duplicate') return '相似圖片'
  if (category === 'similar_video_duplicate') return '相似影片'
  if (category === 'large_video_file') return '大影片'
  if (category === 'bad_video_file') return '壞影片'
  if (category === 'landscape_image') return '風景圖'
  if (category === 'non_portrait_image') return '無人像'
  return category
}

function fileSummary(file: DuplicateCleanerFoundFile): string {
  const parts = [
    (file.categories || []).map(categoryLabel).join(' / '),
    formatFileSize(file.size),
  ].filter(Boolean)
  const similarity =
    typeof file.image_similarity === 'number'
      ? file.image_similarity
      : typeof file.video_similarity === 'number'
        ? file.video_similarity
        : null
  if (similarity !== null) parts.push(`相似度 ${Math.round(similarity)}%`)
  if (file.similar_to) parts.push(`相似於 ${file.similar_to}`)
  if (file.has_person === true) parts.push('偵測到人體')
  if (file.has_face === true) parts.push('偵測到人臉')
  if (file.ai_landscape_detected && typeof file.ai_landscape_score === 'number') {
    parts.push(`風景信心 ${Math.round(file.ai_landscape_score * 100)}%`)
  }
  if (file.video_issue) parts.push(`影片狀態 ${file.video_issue}`)
  if (file.deleted_during_scan && file.kept_path) parts.push(`已直接刪除，保留 ${file.kept_path}`)
  return parts.join(' - ') || '已列出'
}

function progressMessage(progress: DuplicateCleanerProgress | null): string {
  if (!progress) return '等待掃描'
  if (progress.message) return progress.message
  if (progress.phase === 'folder_scan') return `掃描資料夾：${progress.current_folder || ''}`
  if (progress.phase === 'image_analysis') return `分析圖片：${progress.current_file || ''}`
  if (progress.phase === 'video_analysis') return `分析影片：${progress.current_file || ''}`
  return progress.phase || '掃描中'
}

export function DuplicateCleanerWindowApp() {
  const { cancelToolRun, requestToolRun, socketStatus } = useToolRunner(TOOL_ID, 30 * 60 * 1000)
  const [targetDir, setTargetDir] = useState('')
  const [runState, setRunState] = useState<RunState>('idle')
  const [message, setMessage] = useState('自動檔案清理已就緒')
  const [output, setOutput] = useState('')
  const [progress, setProgress] = useState<DuplicateCleanerProgress | null>(null)
  const [foundFiles, setFoundFiles] = useState<DuplicateCleanerFoundFile[]>([])
  const [selectedFiles, setSelectedFiles] = useState<string[]>([])
  const [stopRequested, setStopRequested] = useState(false)

  const [exactDuplicateAnalysis, setExactDuplicateAnalysis] = useState(true)
  const [deleteDuringScan, setDeleteDuringScan] = useState(false)
  const [imageAnalysis, setImageAnalysis] = useState(false)
  const [landscapeFeatures, setLandscapeFeatures] = useState(false)
  const [similarImageAnalysis, setSimilarImageAnalysis] = useState(false)
  const [similarVideoAnalysis, setSimilarVideoAnalysis] = useState(false)
  const [parallelAnalysis, setParallelAnalysis] = useState(true)
  const [faceSensitivity, setFaceSensitivity] = useState(75)
  const [personSensitivity, setPersonSensitivity] = useState(75)
  const [landscapeSensitivity, setLandscapeSensitivity] = useState(65)
  const [analysisSpeed, setAnalysisSpeed] = useState(50)
  const [similarImageThreshold, setSimilarImageThreshold] = useState(96)
  const [similarVideoThreshold, setSimilarVideoThreshold] = useState(96)

  useEffect(() => {
    const handler = (event: Event) => {
      const customEvent = event as CustomEvent
      const detail = customEvent.detail || {}
      if (detail.event !== 'toolbox_run_tool_progress') return
      const payload = (detail.payload || {}) as DuplicateCleanerProgress
      if (payload.tool_id && payload.tool_id !== TOOL_ID) return
      setProgress(payload)
      setMessage(progressMessage(payload))
      if (payload.found_file?.path) {
        setFoundFiles((current) => mergeFoundFiles(current, payload.found_file!))
      }
    }
    window.addEventListener('ipc_event', handler)
    return () => window.removeEventListener('ipc_event', handler)
  }, [])

  const selectedSet = useMemo(() => new Set(selectedFiles), [selectedFiles])
  const exactPaths = useMemo(() => exactDuplicatePaths(foundFiles), [foundFiles])
  const selectedExactFiles = selectedFiles.filter((file) => exactPaths.includes(file))
  const busy = runState === 'running'
  const canRun =
    !busy &&
    targetDir.trim().length > 0 &&
    (exactDuplicateAnalysis || imageAnalysis || similarImageAnalysis || similarVideoAnalysis || deleteDuringScan)
  const folderCurrent = Number(progress?.folder_current || 0)
  const folderTotal = Number(progress?.folder_total || 0)
  const progressPercent =
    folderTotal > 0 ? Math.min(100, Math.round((folderCurrent / folderTotal) * 100)) : runState === 'success' ? 100 : 0

  const chooseTarget = async () => {
    const folder = await selectFolder()
    if (!folder) return
    setTargetDir(folder)
    setFoundFiles([])
    setSelectedFiles([])
    setProgress(null)
    setOutput('')
  }

  const buildArgs = (): string[] => {
    const args = [targetDir.trim(), '--json', '--progress-jsonl']
    const fullAnalysis = !deleteDuringScan
    if (deleteDuringScan) args.push('--delete-exact-during-scan')
    if (!exactDuplicateAnalysis && !deleteDuringScan) args.push('--no-duplicate-analysis')
    if (fullAnalysis && (imageAnalysis || similarImageAnalysis)) {
      args.push('--image-analysis', '--allow-no-face-images')
    }
    if (fullAnalysis && !landscapeFeatures) args.push('--no-ai-landscape-features')
    args.push('--face-sensitivity', String(faceSensitivity))
    args.push('--person-sensitivity', String(personSensitivity))
    args.push('--landscape-sensitivity', String(landscapeSensitivity))
    args.push('--analysis-speed', String(analysisSpeed))
    if (fullAnalysis && !parallelAnalysis) args.push('--no-parallel-analysis')
    if (fullAnalysis && similarImageAnalysis) {
      args.push('--similar-image-analysis', '--similar-image-threshold', String(similarImageThreshold))
    }
    if (fullAnalysis && similarVideoAnalysis) {
      args.push('--similar-video-analysis', '--similar-video-threshold', String(similarVideoThreshold))
    }
    return args
  }

  const runScan = () => {
    if (!canRun) return
    setRunState('running')
    setMessage('正在掃描資料夾...')
    setOutput('')
    setProgress(null)
    setFoundFiles([])
    setSelectedFiles([])
    setStopRequested(false)
    void (async () => {
      try {
        const result = await requestToolRun(buildArgs())
        const report = parseReport(result.stdout)
        const files = foundFilesFromReport(report)
        if (report) {
          setFoundFiles(files)
          setSelectedFiles(exactDuplicatePaths(files))
        }
        if (result.cancelled) {
          setRunState('idle')
          setMessage('掃描已停止。')
          return
        }
        const ok = result.ok !== false
        setRunState(ok ? 'success' : 'error')
        setMessage(ok ? (files.length > 0 ? `找到 ${files.length} 個項目` : '沒有找到需處理項目') : result.message || '掃描失敗')
        setOutput(formatRunOutput(result))
      } catch (error) {
        setRunState('error')
        setMessage(error instanceof Error ? error.message : '掃描逾時')
      } finally {
        setStopRequested(false)
      }
    })()
  }

  const stopScan = () => {
    if (!busy || stopRequested) return
    setStopRequested(true)
    setMessage('正在停止掃描...')
    void cancelToolRun().catch((error) => {
      setStopRequested(false)
      setMessage(error instanceof Error ? error.message : '停止掃描失敗')
    })
  }

  const deleteSelected = () => {
    if (!targetDir.trim() || selectedExactFiles.length === 0 || busy) return
    if (!window.confirm(`確定刪除 ${selectedExactFiles.length} 個已驗證的完全相同重複檔？`)) return
    setRunState('running')
    setMessage('正在刪除選取檔案...')
    void (async () => {
      try {
        const result = await requestToolRun([
          targetDir.trim(),
          '--delete-selected-json',
          JSON.stringify(selectedExactFiles),
          '--json',
        ])
        const report = parseReport(result.stdout)
        const deleted = new Set(Array.isArray(report?.deleted_files) ? report.deleted_files : [])
        const ok = result.ok !== false
        setRunState(ok ? 'success' : 'error')
        if (ok) {
          setFoundFiles((current) => current.filter((file) => !deleted.has(file.path)))
          setSelectedFiles([])
        }
        setMessage(
          ok
            ? `已刪除 ${report?.deleted_file_count ?? deleted.size} 個，略過 ${report?.skipped_file_count ?? 0} 個`
            : result.message || '刪除失敗'
        )
        setOutput(formatRunOutput(result))
      } catch (error) {
        setRunState('error')
        setMessage(error instanceof Error ? error.message : '刪除逾時')
      }
    })()
  }

  const revealFile = async (file: DuplicateCleanerFoundFile, mode: 'open' | 'reveal') => {
    const payload = isAbsoluteFilesystemPath(file.path)
      ? { path: file.path, mode }
      : { basePath: targetDir.trim(), relativePath: file.path, mode }
    const result = await openPath(payload)
    if (result.ok === false) setMessage(result.message || '開啟路徑失敗')
  }

  return (
    <main style={styles.app}>
      <section style={styles.card}>
        <header style={styles.header}>
          <div>
            <div style={styles.kicker}>Standalone Application</div>
            <h1 style={styles.title}>自動檔案清理</h1>
            <p style={styles.muted}>逐資料夾掃描重複檔、無人像圖片、風景圖與影片問題。</p>
          </div>
          <span style={styles.badge}>{socketStatus}</span>
        </header>

        <div style={styles.fieldGroup}>
          <label style={styles.label}>目標資料夾</label>
          <div style={styles.inlineRow}>
            <input
              value={targetDir}
              onChange={(event) => setTargetDir(event.target.value)}
              placeholder="選擇或貼上要掃描的資料夾"
              style={styles.input}
            />
            <button type="button" onClick={chooseTarget} style={styles.secondaryButton}>
              選擇
            </button>
          </div>
        </div>

        <section style={styles.notice}>
          <label style={styles.checkboxRow}>
            <input type="checkbox" checked={deleteDuringScan || exactDuplicateAnalysis} disabled={deleteDuringScan} onChange={(event) => setExactDuplicateAnalysis(event.target.checked)} />
            <strong>完全相同重複檔</strong>
          </label>
          <label style={{ ...styles.checkboxRow, marginTop: 10 }}>
            <input
              type="checkbox"
              checked={deleteDuringScan}
              onChange={(event) => {
                const enabled = event.target.checked
                setDeleteDuringScan(enabled)
                if (enabled) {
                  setExactDuplicateAnalysis(true)
                  setImageAnalysis(false)
                  setLandscapeFeatures(false)
                  setSimilarImageAnalysis(false)
                  setSimilarVideoAnalysis(false)
                }
              }}
            />
            <strong>完全相同重複檔直接刪除並列出</strong>
          </label>
        </section>

        <section style={styles.notice}>
          <label style={styles.checkboxRow}>
            <input type="checkbox" checked={!deleteDuringScan && imageAnalysis} disabled={deleteDuringScan} onChange={(event) => setImageAnalysis(event.target.checked)} />
            <strong>無人像圖片分析</strong>
          </label>
          <label style={{ ...styles.checkboxRow, marginTop: 10, opacity: !deleteDuringScan && imageAnalysis ? 1 : 0.55 }}>
            <input type="checkbox" checked={!deleteDuringScan && landscapeFeatures} disabled={deleteDuringScan || !imageAnalysis} onChange={(event) => setLandscapeFeatures(event.target.checked)} />
            <strong>風景圖語意特徵分析</strong>
          </label>
          <label style={{ ...styles.checkboxRow, marginTop: 10 }}>
            <input type="checkbox" checked={!deleteDuringScan && similarImageAnalysis} disabled={deleteDuringScan} onChange={(event) => setSimilarImageAnalysis(event.target.checked)} />
            <strong>重複圖片相似度</strong>
          </label>
          <label style={{ ...styles.checkboxRow, marginTop: 10 }}>
            <input type="checkbox" checked={!deleteDuringScan && similarVideoAnalysis} disabled={deleteDuringScan} onChange={(event) => setSimilarVideoAnalysis(event.target.checked)} />
            <strong>重複影片相似度</strong>
          </label>
          <label style={{ ...styles.checkboxRow, marginTop: 10 }}>
            <input type="checkbox" checked={!deleteDuringScan && parallelAnalysis} disabled={deleteDuringScan} onChange={(event) => setParallelAnalysis(event.target.checked)} />
            <strong>多工分析</strong>
          </label>

          <div style={styles.sliderGrid}>
            {[
              ['人臉', faceSensitivity, setFaceSensitivity],
              ['人體', personSensitivity, setPersonSensitivity],
              ['風景', landscapeSensitivity, setLandscapeSensitivity],
              ['速度', analysisSpeed, setAnalysisSpeed],
              ['重複圖片', similarImageThreshold, setSimilarImageThreshold],
              ['重複影片', similarVideoThreshold, setSimilarVideoThreshold],
            ].map(([label, value, setter]) => (
              <label key={String(label)} style={styles.sliderControl}>
                <span style={styles.sliderHeader}>
                  <span>{String(label)}</span>
                  <strong>{Number(value)}%</strong>
                </span>
                <input
                  type="range"
                  min={1}
                  max={100}
                  value={Number(value)}
                  disabled={deleteDuringScan}
                  onChange={(event) => (setter as (value: number) => void)(Number(event.target.value))}
                  style={styles.rangeInput}
                />
              </label>
            ))}
          </div>
        </section>

        <div style={styles.actions}>
          {busy ? (
            <button type="button" disabled={stopRequested} onClick={stopScan} style={styles.dangerButton}>
              {stopRequested ? '停止中...' : '停止掃描'}
            </button>
          ) : null}
          <button type="button" disabled={!canRun} onClick={runScan} style={styles.primaryButton}>
            {busy ? '掃描中...' : '開始掃描'}
          </button>
        </div>

        <section style={styles.resultPanel}>
          <div style={{ ...styles.statusLine, justifyContent: 'space-between' }}>
            <strong>
              資料夾 {folderCurrent}/{folderTotal || '?'}
            </strong>
            <span>{progressPercent}%</span>
          </div>
          <div style={{ height: 8, background: '#1e293b', borderRadius: 999, overflow: 'hidden' }}>
            <div style={{ height: '100%', width: `${progressPercent}%`, background: '#7dd3fc' }} />
          </div>
          <p style={styles.noticeText}>
            {progressMessage(progress)} - 檔案 {progress?.source_file_count ?? 0} / 找到{' '}
            {progress?.found_file_count ?? foundFiles.length}
          </p>
        </section>

        {foundFiles.length > 0 ? (
          <section style={styles.resultPanel}>
            <div style={{ ...styles.statusLine, justifyContent: 'space-between' }}>
              <strong>
                找到 {foundFiles.length} 項，已選 {selectedExactFiles.length} 個可刪除重複檔
              </strong>
              <button type="button" onClick={deleteSelected} disabled={selectedExactFiles.length === 0 || busy} style={styles.dangerButton}>
                刪除選取
              </button>
            </div>
            <div style={styles.resultList}>
              {foundFiles.map((file) => {
                const selectable = isExactDuplicate(file)
                return (
                  <div key={file.path} style={styles.resultRow}>
                    <input
                      type="checkbox"
                      checked={selectable && selectedSet.has(file.path)}
                      disabled={!selectable}
                      onChange={(event) =>
                        setSelectedFiles((current) =>
                          event.target.checked
                            ? Array.from(new Set([...current, file.path]))
                            : current.filter((item) => item !== file.path)
                        )
                      }
                    />
                    {file.thumbnail ? (
                      <img alt="" src={file.thumbnail} style={{ width: 54, height: 54, objectFit: 'cover', borderRadius: 8 }} />
                    ) : null}
                    <span style={styles.resultText}>
                      <span style={styles.resultPath}>{file.path}</span>
                      <span style={styles.resultMeta}>{fileSummary(file)}</span>
                    </span>
                    <button type="button" onClick={() => void revealFile(file, 'open')} style={styles.secondaryButton}>
                      開啟
                    </button>
                    <button type="button" onClick={() => void revealFile(file, 'reveal')} style={styles.secondaryButton}>
                      位置
                    </button>
                  </div>
                )
              })}
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
          <pre style={styles.output}>{output || '尚無輸出。'}</pre>
        </section>
      </section>
    </main>
  )
}
