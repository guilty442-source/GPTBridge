import React from 'react'
import type { RuntimeService } from '@/shared/types/runtime'
import { zhTW } from '@/i18n/zhTW'

interface RuntimeDiagnosticsProps {
  startupError?: string | null
  bootLogs?: string[]
  systemStatus?: string
  services?: RuntimeService[]
}

export const RuntimeDiagnostics: React.FC<RuntimeDiagnosticsProps> = ({
  startupError,
  bootLogs = [],
  services = [],
  systemStatus,
}) => {
  const logsToDisplay =
    bootLogs.length > 0
      ? bootLogs
      : [
          `[系統] ${zhTW.developer.title}首頁已載入`,
          '[系統] 診斷監控已就緒',
          '[系統] 治理觀測流程已掛載',
        ]

  return (
    <section className="devm-diagnostics">
      <div className="devm-diagnostics-header">
        <strong className="devm-diagnostics-title">系統診斷</strong>
        <span className="devm-pill">{systemStatus || 'UNKNOWN'}</span>
      </div>

      {startupError ? (
        <div className="devm-startup-error">
          <strong>啟動錯誤：</strong>
          {startupError}
        </div>
      ) : null}

      <pre className="devm-log-box">
        {logsToDisplay.map((log, index) => (
          <div key={index}>{log}</div>
        ))}
      </pre>
    </section>
  )
}
