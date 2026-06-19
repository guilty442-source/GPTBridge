import React, { useEffect, useMemo, useState } from 'react'
import { hmrService, type HMRInspectorSnapshot } from '@/shared/services/hmrService'

function modeLabel(snapshot: HMRInspectorSnapshot): string {
  if (snapshot.mode === 'force-restart') return '強制重啟保護中'
  if (snapshot.mode === 'recovering') return '修復中'
  if (snapshot.mode === 'warning') return '警示'
  return '穩定'
}

function tone(snapshot: HMRInspectorSnapshot): 'ok' | 'warn' | 'error' {
  if (snapshot.level >= 4) return 'error'
  if (snapshot.level >= 2) return 'warn'
  return 'ok'
}

export const HMRGovernanceCard: React.FC = () => {
  const [snapshot, setSnapshot] = useState<HMRInspectorSnapshot>(
    hmrService.getSnapshot()
  )

  useEffect(() => {
    return hmrService.subscribe(setSnapshot)
  }, [])

  const lastEvent = useMemo(() => {
    if (!snapshot.lastEventAt) return '尚無事件'
    return new Date(snapshot.lastEventAt).toLocaleTimeString('zh-TW', {
      hour12: false,
    })
  }, [snapshot.lastEventAt])

  return (
    <article className="devm-card">
      <h3 className="devm-card-title">HMR 治理</h3>
      <p className="devm-card-body">
        監控熱更新連線品質，若連續失敗會自動升級為重載或應用程式重啟。
      </p>

      <div className="devm-keyval">
        <div className="devm-keyval-row">
          <span>目前狀態</span>
          <b>
            <span className={`devm-pill devm-pill--${tone(snapshot)}`}>
              <span className="devm-pill-dot" />
              {modeLabel(snapshot)}
            </span>
          </b>
        </div>
        <div className="devm-keyval-row">
          <span>等級 / 失敗次數</span>
          <b>
            Lv.{snapshot.level} / {snapshot.failureCount}
          </b>
        </div>
        <div className="devm-keyval-row">
          <span>最後事件</span>
          <b>{lastEvent}</b>
        </div>
      </div>

      <button
        type="button"
        className="devm-secondary-btn"
        disabled={snapshot.level <= 0}
        onClick={() => hmrService.clearLevel()}
      >
        清除 HMR 警示等級
      </button>
    </article>
  )
}
