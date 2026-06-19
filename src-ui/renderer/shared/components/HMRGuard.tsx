import React, { useEffect, useState } from 'react'
import {
  type HMRInspectorSnapshot,
  hmrService,
} from '../services/hmrService'

function getOverlayMessage(snapshot: HMRInspectorSnapshot): string {
  if (snapshot.level >= 5) return '熱更新復原失敗，正在強制重啟應用程式...'
  if (snapshot.level >= 4) return '偵測到大型變更，正在強制重載視窗...'
  if (snapshot.level >= 3) return '熱更新連線異常，正在重新載入視窗...'
  if (snapshot.level >= 2) return '正在嘗試修復熱更新狀態...'
  return '熱更新監控中'
}

export function HMRGuard({ children }: { children: React.ReactNode }) {
  const [snapshot, setSnapshot] = useState<HMRInspectorSnapshot>(
    hmrService.getSnapshot()
  )

  useEffect(() => {
    hmrService.init()
    return hmrService.subscribe(setSnapshot)
  }, [])

  if (snapshot.level < 2) {
    return <>{children}</>
  }

  return (
    <div style={overlayStyle}>
      <div style={panelStyle}>
        <div style={titleStyle}>HMR 檢察系統</div>
        <p style={messageStyle}>{getOverlayMessage(snapshot)}</p>
        <p style={detailStyle}>原因：{snapshot.lastReason || '未知錯誤'}</p>
        <p style={detailStyle}>失敗次數：{snapshot.failureCount}</p>
        <button
          type="button"
          style={restartButtonStyle}
          onClick={() => {
            void hmrService.forceRestart('manual force restart from guard overlay')
          }}
        >
          立即強制重啟
        </button>
      </div>
    </div>
  )
}

const overlayStyle: React.CSSProperties = {
  position: 'fixed',
  inset: 0,
  backgroundColor: 'rgba(2, 6, 23, 0.88)',
  color: '#fff',
  display: 'flex',
  justifyContent: 'center',
  alignItems: 'center',
  zIndex: 9999,
  fontFamily: 'Noto Sans TC, Inter, sans-serif',
}

const panelStyle: React.CSSProperties = {
  width: 'min(560px, 92vw)',
  border: '1px solid #334155',
  borderRadius: '14px',
  background: '#0f172a',
  padding: '20px 22px',
  boxShadow: '0 20px 40px rgba(0, 0, 0, 0.45)',
}

const titleStyle: React.CSSProperties = {
  fontSize: '18px',
  fontWeight: 800,
  marginBottom: '12px',
}

const messageStyle: React.CSSProperties = {
  margin: 0,
  color: '#e2e8f0',
  fontSize: '14px',
  fontWeight: 600,
}

const detailStyle: React.CSSProperties = {
  margin: '8px 0 0',
  color: '#94a3b8',
  fontSize: '12px',
}

const restartButtonStyle: React.CSSProperties = {
  marginTop: '16px',
  padding: '10px 14px',
  border: '1px solid #ef4444',
  borderRadius: '10px',
  background: '#7f1d1d',
  color: '#fff',
  fontWeight: 700,
  cursor: 'pointer',
}
