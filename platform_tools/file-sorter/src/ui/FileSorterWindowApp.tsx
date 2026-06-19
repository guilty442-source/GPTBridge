import { useMemo, useState } from 'react'
import {
  formatRunOutput,
  selectFolder,
  toolWindowStyles as styles,
  useToolRunner,
} from './toolWindowRunner'

type RunState = 'idle' | 'running' | 'success' | 'error'

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

export function FileSorterWindowApp() {
  const { requestToolRun, socketStatus } = useToolRunner('file-sorter')
  const [targetDir, setTargetDir] = useState('')
  const [keywordInput, setKeywordInput] = useState('')
  const [keywordFolder, setKeywordFolder] = useState('')
  const [currentKeyword, setCurrentKeyword] = useState('')
  const [updatedKeyword, setUpdatedKeyword] = useState('')
  const [runState, setRunState] = useState<RunState>('idle')
  const [message, setMessage] = useState('自動化檔案管理已就緒')
  const [output, setOutput] = useState('')

  const keywords = useMemo(() => parseKeywords(keywordInput), [keywordInput])
  const busy = runState === 'running'
  const canRun = !busy && targetDir.trim().length > 0

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
      setMessage(error instanceof Error ? error.message : '工具執行逾時')
      return null
    }
  }

  const chooseTarget = async () => {
    const folder = await selectFolder()
    if (folder) setTargetDir(folder)
  }

  const runSorter = () => {
    if (!canRun) return
    void execute([targetDir.trim()], '正在自動分類檔案...', '自動分類完成')
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
    void execute(args, '正在新增或更新關鍵字規則...', '關鍵字規則已更新')
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
    void execute(args, '正在修改關鍵字規則...', '關鍵字規則已修改')
  }

  return (
    <main style={styles.app}>
      <section style={styles.card}>
        <header style={styles.header}>
          <div>
            <div style={styles.kicker}>Standalone Application</div>
            <h1 style={styles.title}>自動化檔案管理</h1>
            <p style={styles.muted}>依照關鍵字規則整理檔案，支援跨硬碟目的地。</p>
          </div>
          <span style={styles.badge}>{socketStatus}</span>
        </header>

        <div style={styles.fieldGroup}>
          <label style={styles.label}>目標資料夾</label>
          <div style={styles.inlineRow}>
            <input
              value={targetDir}
              onChange={(event) => setTargetDir(event.target.value)}
              placeholder="選擇或貼上要整理的資料夾"
              style={styles.input}
            />
            <button type="button" onClick={chooseTarget} style={styles.secondaryButton}>
              選擇
            </button>
          </div>
        </div>

        <section style={styles.notice}>
          <strong>關鍵字規則</strong>
          <p style={styles.noticeText}>輸入多個關鍵字時可用換行或逗號分隔。</p>
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
            <input
              value={keywordFolder}
              onChange={(event) => setKeywordFolder(event.target.value)}
              placeholder="目標資料夾內的子資料夾或完整路徑"
              style={styles.input}
            />
          </div>
          <div style={styles.actions}>
            <button type="button" disabled={busy} onClick={listKeywords} style={styles.secondaryButton}>
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
            <button type="button" disabled={busy} onClick={updateKeyword} style={styles.secondaryButton}>
              修改
            </button>
          </div>
        </section>

        <div style={styles.actions}>
          <button type="button" disabled={!canRun} onClick={runSorter} style={styles.primaryButton}>
            {busy ? '執行中...' : '開始整理'}
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
          <pre style={styles.output}>{output || '尚無輸出。'}</pre>
        </section>
      </section>
    </main>
  )
}
