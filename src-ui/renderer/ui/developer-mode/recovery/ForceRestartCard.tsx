import React from 'react'

interface ForceRestartCardProps {
  onRestart: () => void
}

export const ForceRestartCard: React.FC<ForceRestartCardProps> = ({ onRestart }) => {
  return (
    <article className="devm-card devm-card-danger">
      <h3 className="devm-card-title">強制重啟</h3>
      <p className="devm-card-body">
        當熱更新無法復原或大型變更無法套用時，可使用強制重啟重新建立乾淨狀態。
      </p>

      <ul className="devm-list">
        <li>會中止目前進行中的背景流程</li>
        <li>會關閉現有視窗並重新啟動應用程式</li>
        <li>建議先保存重要輸入內容</li>
      </ul>

      <button type="button" className="devm-action-btn" onClick={onRestart}>
        立即重啟系統
      </button>
    </article>
  )
}
