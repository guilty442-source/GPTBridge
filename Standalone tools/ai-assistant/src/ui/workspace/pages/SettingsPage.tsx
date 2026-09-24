import React, { useEffect, useState } from 'react'
import { useCommandQuery, useIpcEvent } from '../hooks'
import { EmptyState, Section, Badge } from '../common'
import { useNotify } from '../uiState'

type Props = { sendCommand: (c: string, p?: unknown) => { ok: boolean; message?: string } }

/** Only whitelisted display-preference keys are writable through this
 * page — trading authorizations, risk limits, LIVE and broker flags are
 * never settable here (backend enforces; see asset_mgmt domain). */
const WRITABLE: { key: string; label: string; choices?: string[]; type?: string }[] = [
  { key: 'display_currency', label: '顯示幣別', choices: ['TWD', 'USD'] },
  { key: 'theme', label: '主題', choices: ['light', 'dark'] },
  { key: 'font_scale', label: '字體縮放', type: 'number' },
  { key: 'sidebar_collapsed', label: '側邊欄收合', choices: ['false', 'true'] },
  { key: 'default_page', label: '預設頁面' },
  { key: 'watchlist_tw', label: '台股自選清單（逗號分隔）' },
  { key: 'watchlist_us', label: '美股自選清單（逗號分隔）' },
  { key: 'notification_mute', label: '通知靜音', choices: ['false', 'true'] },
]

/** Categories the engine does not yet expose — shown honestly as
 * 整合未完成 instead of fake controls. */
const NOT_INTEGRATED = [
  '帳戶管理（引擎端管理）',
  '行情更新間隔',
  '基金更新排程',
  '星澄模型通道設定',
  '分析排程',
  '策略參數',
  '資料匯入匯出設定',
]

export default function SettingsPage({ sendCommand }: Props) {
  const notify = useNotify()
  const get = useCommandQuery<any>(sendCommand, 'investment_settings_get')
  const [draft, setDraft] = useState<Record<string, unknown>>({})

  useEffect(() => {
    if (get.data?.settings) setDraft(get.data.settings)
  }, [get.data])

  useIpcEvent('investment_settings_set_result', (p) => {
    const d = p as Record<string, unknown>
    notify(
      d?.ok === false
        ? `設定未儲存：${d.error_code || d.message}${
            Array.isArray(d?.denied) ? `（${d.denied.join(', ')}）` : ''}`
        : '設定已儲存',
      d?.ok === false ? 'WARNING' : 'INFO',
    )
  })

  const save = () => {
    const ack = sendCommand('investment_settings_set', { settings: draft })
    if (!ack.ok) notify(`設定未送出：${ack.message}`, 'ERROR')
  }

  const s = draft as Record<string, any>
  const writable = new Set<string>(
    Array.isArray(get.data?.writable_keys)
      ? get.data.writable_keys.map(String)
      : WRITABLE.map((w) => w.key),
  )

  return (
    <div className="inv-page">
      <header className="inv-page-head">
        <h2>系統設定</h2>
        <button className="inv-primary" onClick={save}>儲存</button>
      </header>

      <Section title="一般與顯示設定">
        <div className="inv-form">
          {WRITABLE.filter((w) => writable.has(w.key)).map((w) => (
            <label key={w.key}>{w.label}
              {w.choices ? (
                <select value={String(s[w.key] ?? '')}
                  onChange={(e) =>
                    setDraft({ ...draft, [w.key]: e.target.value })}>
                  <option value="">—</option>
                  {w.choices.map((c) => <option key={c} value={c}>{c}</option>)}
                </select>
              ) : (
                <input type={w.type || 'text'}
                  value={String(s[w.key] ?? '')}
                  onChange={(e) =>
                    setDraft({
                      ...draft,
                      [w.key]: w.type === 'number'
                        ? Number(e.target.value)
                        : e.target.value,
                    })} />
              )}
            </label>
          ))}
        </div>
        {get.data?.note && <p className="inv-note">{String(get.data.note)}</p>}
      </Section>

      <Section title="尚未整合的設定類別">
        <ul className="inv-denied">
          {NOT_INTEGRATED.map((k) => (
            <li key={k}><Badge text={`${k}——整合未完成`} tone="muted" /></li>
          ))}
        </ul>
      </Section>

      <Section title="受治理保護（不可經此頁修改）">
        <ul className="inv-denied">
          {[
            '正式交易授權', '風控上限', 'LIVE 模式', 'broker_network_enabled',
            '券商憑證', 'RUNNING 策略正式版本',
          ].map((k) => (
            <li key={k}><Badge text={k} tone="danger" /></li>
          ))}
        </ul>
        <p className="inv-note">
          後端拒絕非白名單鍵（SETTING_KEY_DENIED）；前端欄位非安全防護。
        </p>
      </Section>
    </div>
  )
}
