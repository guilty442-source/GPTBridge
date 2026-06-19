import React, { useMemo } from 'react'

export const EnvironmentCard: React.FC = () => {
  const facts = useMemo(() => {
    const mode = (import.meta as any).env?.MODE ?? 'unknown'
    const hmrEnabled = Boolean((import.meta as any).hot)
    const electronReady = Boolean((window as any).electron?.invoke)

    return [
      { key: 'mode', label: 'NODE_ENV', value: mode },
      { key: 'hmr', label: 'HMR', value: hmrEnabled ? '已啟用' : '未啟用' },
      { key: 'electron', label: 'Electron Bridge', value: electronReady ? '已連接' : '未連接' },
      { key: 'platform', label: 'Platform', value: navigator.platform || 'unknown' },
    ]
  }, [])

  return (
    <article className="devm-card">
      <h3 className="devm-card-title">目前環境</h3>
      <p className="devm-card-body">確認前端執行上下文與橋接狀態，避免環境不一致造成誤判。</p>

      <div className="devm-keyval">
        {facts.map((fact) => (
          <div key={fact.key} className="devm-keyval-row">
            <span>{fact.label}</span>
            <b>{fact.value}</b>
          </div>
        ))}
      </div>
    </article>
  )
}
