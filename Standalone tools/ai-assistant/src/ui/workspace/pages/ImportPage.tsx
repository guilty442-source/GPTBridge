import React, { useMemo, useState } from 'react'
import { useCommandQuery } from '../hooks'
import { DataFreshnessIndicator } from '../freshness'
import { DataTable, EmptyState, Section, Badge } from '../common'
import { useNotify } from '../uiState'

type Props = { sendCommand: (c: string, p?: unknown) => { ok: boolean; message?: string } }

const arr = (v: unknown): Record<string, unknown>[] =>
  Array.isArray(v) ? (v as Record<string, unknown>[]) : []

const TARGETS = [
  { key: 'tw', label: '國泰台股持倉' },
  { key: 'us', label: '富邦美股持倉' },
  { key: 'fund', label: '共同基金' },
  { key: 'txn', label: '交易歷史' },
  { key: 'dividend', label: '股息' },
  { key: 'distribution', label: '配息' },
]

/** Client-side preview only — parses the chosen file into rows for the
 * mapping/preview/diff steps. Actual import executes in the engine; the
 * original file is never modified here. */
function parsePreview(name: string, text: string): Record<string, unknown>[] {
  if (name.endsWith('.json')) {
    try {
      const d = JSON.parse(text)
      return arr(Array.isArray(d) ? d : d.rows || d.records).slice(0, 50)
    } catch {
      return []
    }
  }
  // CSV preview (XLSX needs the engine-side parser — preview is text-based)
  const lines = text.split(/\r?\n/).filter(Boolean)
  if (!lines.length) return []
  const head = lines[0].split(',').map((s) => s.trim())
  return lines.slice(1, 51).map((l) => {
    const cells = l.split(',')
    return Object.fromEntries(head.map((h, i) => [h, cells[i]?.trim() ?? '']))
  })
}

export default function ImportPage({ sendCommand }: Props) {
  const notify = useNotify()
  const [step, setStep] = useState(0)
  const [target, setTarget] = useState('tw')
  const [fileName, setFileName] = useState('')
  const [rows, setRows] = useState<Record<string, unknown>[]>([])
  const [mapping, setMapping] = useState<Record<string, string>>({})
  const [submitResult, setSubmitResult] = useState('')

  const history = useCommandQuery<any>(sendCommand, 'investment_broker_imports')

  const columns = useMemo(
    () => (rows.length ? Object.keys(rows[0]) : []),
    [rows],
  )

  const onFile = async (f: File) => {
    setFileName(f.name)
    const text = await f.text()
    const preview = f.name.endsWith('.xlsx')
      ? [] // engine-side parser required
      : parsePreview(f.name, text)
    setRows(preview)
    setStep(f.name.endsWith('.xlsx') ? 1 : 2)
    if (f.name.endsWith('.xlsx'))
      setSubmitResult('XLSX 需由引擎端解析器處理——此介面僅能預覽 CSV/JSON。')
  }

  const submit = () => {
    // The governed channel: broker imports run in investment-mobile.
    // ai-assistant holds no inbound route — we submit the intent and
    // surface the honest result.
    const ack = sendCommand('investment_broker_import_submit', {
      target, file_name: fileName, mapping,
      preview_rows: rows.length,
    })
    setSubmitResult(
      ack.ok
        ? '已送出——等待引擎端結果。'
        : `無法送出：${ack.message}（整合未完成——匯入執行於投資引擎端）`,
    )
    notify(submitResult || '匯入請求已送出', ack.ok ? 'INFO' : 'WARNING')
  }

  const steps = ['選擇檔案', '格式辨識', '欄位對應', '資料預覽',
    '錯誤檢查', '差異顯示', '使用者確認', '正式匯入']

  return (
    <div className="inv-page">
      <header className="inv-page-head">
        <h2>資料匯入中心</h2>
        <span className="inv-badge inv-badge-info">支援 CSV / XLSX / JSON</span>
      </header>

      <Section title="匯入流程">
        <ol className="inv-steps">
          {steps.map((s, i) => (
            <li key={s} className={i === step ? 'active' : i < step ? 'done' : ''}>
              {s}
            </li>
          ))}
        </ol>

        <div className="inv-form">
          <label>匯入目標
            <select value={target} onChange={(e) => setTarget(e.target.value)}>
              {TARGETS.map((t) => (
                <option key={t.key} value={t.key}>{t.label}</option>
              ))}
            </select>
          </label>
          <label>檔案
            <input type="file" accept=".csv,.json,.xlsx"
              onChange={(e) => e.target.files?.[0] && onFile(e.target.files[0])} />
          </label>
        </div>

        {step >= 2 && rows.length > 0 && (
          <>
            <Section title="欄位對應">
              <div className="inv-form">
                {columns.map((c) => (
                  <label key={c}>{c}
                    <input value={mapping[c] || c}
                      onChange={(e) =>
                        setMapping({ ...mapping, [c]: e.target.value })} />
                  </label>
                ))}
              </div>
            </Section>
            <Section title="資料預覽（前 50 筆）">
              <DataTable
                rows={rows}
                columns={columns.map((c) => ({ key: c, label: c }))}
                pageSize={10}
              />
              <div className="inv-actions">
                <button className="inv-primary" onClick={() => setStep(6)}>
                  檢查完成——前往確認
                </button>
              </div>
            </Section>
          </>
        )}

        {step >= 6 && (
          <Section title="確認匯入">
            <p>檔案：{fileName}　目標：{TARGETS.find((t) => t.key === target)?.label}
              　預覽筆數：{rows.length}</p>
            <p className="inv-note">
              重複資料與衝突警告由引擎端檢查；原始檔案不會被改寫。
            </p>
            <button className="inv-primary" onClick={submit}>確認匯入</button>
          </Section>
        )}
        {submitResult && <p className="inv-note inv-ctrl-result">{submitResult}</p>}
      </Section>

      <Section title="匯入歷史">
        <DataTable
          rows={arr(history.data?.imports || history.data?.batches)}
          columns={[
            { key: 'batch_id', label: '批次' },
            { key: 'source', label: '來源' },
            { key: 'target', label: '目標' },
            { key: 'rows', label: '筆數' },
            { key: 'warnings', label: '警告' },
            { key: 'status', label: '狀態',
              render: (r) => <Badge text={String(r.status || '—')} /> },
            { key: 'at', label: '時間',
              render: (r) => <DataFreshnessIndicator record={r} /> },
            { key: 'rollback', label: '',
              render: (r) =>
                r.rollback_available ? (
                  <button onClick={() =>
                    sendCommand('investment_broker_import_rollback',
                      { batch_id: r.batch_id })
                  }>受控復原</button>
                ) : null },
          ]}
          empty={<EmptyState detail="尚無匯入歷史。" />}
        />
      </Section>
    </div>
  )
}
