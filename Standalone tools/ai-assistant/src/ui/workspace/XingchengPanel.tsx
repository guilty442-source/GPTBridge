import React, { useMemo, useState } from 'react'
import { useCommandQuery } from './hooks'
import { useUIState } from './uiState'
import { EmptyState } from './common'

type Props = {
  sendCommand: (command: string, payload?: unknown) => { ok: boolean; message?: string }
}

/** 星澄 analysis entry points — natural-language intents map to
 * governed commands; every answer arrives through the backend with
 * deterministic data attached, never raw model text alone. */
const INTENTS: { key: string; label: string; command: string }[] = [
  { key: 'tw', label: '分析我的台股', command: 'investment_tw_analysis' },
  { key: 'us', label: '分析我的美股', command: 'investment_us_analysis' },
  { key: 'fund', label: '分析共同基金', command: 'investment_fund_analysis' },
  { key: 'all', label: '分析總資產', command: 'investment_ai_portfolio_analysis' },
  { key: 'overlap', label: '找出持倉重疊', command: 'investment_assets_exposure' },
  { key: 'risk', label: '分析市場風險', command: 'investment_ai_risk' },
  { key: 'report', label: '產生投資報告', command: 'investment_ai_report' },
  { key: 'sim', label: '檢視模擬交易表現', command: 'investment_sim_performance' },
]

export function XingchengPanel({ sendCommand }: Props) {
  const { state } = useUIState()
  const [question, setQuestion] = useState('')
  const [history, setHistory] = useState<
    { q: string; event: string; at: number }[]
  >([])
  const [last, setLast] = useState<unknown>(null)

  const ask = (label: string, command: string, extra?: object) => {
    const ack = sendCommand(command, extra ?? {})
    setHistory((h) =>
      [{ q: label, event: `${command}_result`, at: Date.now() }, ...h].slice(0, 20),
    )
    if (!ack.ok) setLast({ ok: false, message: ack.message })
  }

  // capture whichever command result arrives last
  React.useEffect(() => {
    const onEvent = (e: Event) => {
      const d = (e as CustomEvent).detail as { event?: string; payload?: unknown }
      if (d?.event && d.event.endsWith('_result')) setLast(d.payload)
    }
    window.addEventListener('ipc_event', onEvent)
    return () => window.removeEventListener('ipc_event', onEvent)
  }, [])

  const freeText = () => {
    const q = question.trim()
    if (!q) return
    // free-form research questions go through the governed search/consult
    // path — the model never reads its private store directly
    ask(q, 'investment_ai_research_search', { query: q })
    setQuestion('')
  }

  const text = useMemo(() => {
    if (last == null) return ''
    const p = last as Record<string, unknown>
    if (p.ok === false)
      return `無法完成：${String(p.error_code || p.message || 'BACKEND_ERROR')}`
    return JSON.stringify(p, null, 2)
  }, [last])

  return (
    <aside className="xing-panel">
      <header className="xing-panel-head">
        <b>星澄 AI 投資助手</b>
        <span className="xing-model-state">
          模型狀態：{state.mode === 'OFFLINE' ? '離線' : '連線'}
        </span>
      </header>
      <div className="xing-intents">
        {INTENTS.map((i) => (
          <button key={i.key} onClick={() => ask(i.label, i.command)}>
            {i.label}
          </button>
        ))}
      </div>
      <div className="xing-input">
        <input
          value={question}
          placeholder="向星澄提問（市場研究）…"
          onChange={(e) => setQuestion(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && freeText()}
        />
        <button onClick={freeText}>送出</button>
      </div>
      <div className="xing-answer">
        {text ? (
          <pre>{text}</pre>
        ) : (
          <EmptyState detail="選擇分析指令或輸入問題；所有數值來自正式資料與確定性計算。" />
        )}
      </div>
      <ul className="xing-history">
        {history.map((h, i) => (
          <li key={i}>
            {new Date(h.at).toLocaleTimeString('zh-TW', { hour12: false })} — {h.q}
          </li>
        ))}
      </ul>
    </aside>
  )
}
