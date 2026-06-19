import { zhTW } from '@/i18n/zhTW'
import type { TaskProgress } from '@/types/ui'
import React, { useEffect, useRef, useState } from 'react'
import { LoadingSpinner } from './LoadingSpinner'

type DeveloperModePanelProps = {
  sendCommand: (command: string, payload?: Record<string, unknown>) => void
  progress?: TaskProgress
  logs?: string[]
}

export const DeveloperModePanel: React.FC<DeveloperModePanelProps> = ({
  sendCommand,
  progress,
  logs = [],
}) => {
  const [copied, setCopied] = useState(false)
  const logContainerRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (logContainerRef.current) {
      requestAnimationFrame(() => {
        if (logContainerRef.current) {
          logContainerRef.current.scrollTop =
            logContainerRef.current.scrollHeight
        }
      })
    }
  }, [logs, progress])

  const handleStart = () => {
    sendCommand('developer_auto_optimize')
  }

  const handleTerminate = () => {
    sendCommand('developer_ai_stop')
  }

  const getLogColor = (line: string) => {
    if (
      line.includes('FAILED') ||
      line.includes('ERROR') ||
      line.includes('Error') ||
      line.includes('failed')
    )
      return '#f87171'
    if (
      line.includes('OK') ||
      line.includes('SUCCESS') ||
      line.includes('done')
    )
      return '#34d399'
    if (line.includes('Gemini')) return '#60a5fa'
    if (line.includes('WebSocket') || line.includes('socket')) return '#fbbf24'
    if (line.includes('[command]')) return '#9ca3af'
    return '#f8fafc'
  }

  const isRunning =
    progress?.status === 'running' &&
    progress?.command === 'developer_auto_optimize'

  return (
    <section
      className="section developer-mode-panel"
      aria-labelledby="dev-panel-title"
    >
      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          marginBottom: '16px',
        }}
      >
        <h2 id="dev-panel-title" style={{ fontSize: '16px', margin: 0 }}>
          {zhTW.developer.autoOptimize}
        </h2>
        {logs.length > 0 && (
          <button
            type="button"
            className="btn-base"
            style={{ padding: '4px 12px', fontSize: '12px' }}
            onClick={() => {
              navigator.clipboard.writeText(logs.join('\n'))
              setCopied(true)
              setTimeout(() => setCopied(false), 2000)
            }}
          >
            {copied ? '已複製！' : '複製全部日誌'}
          </button>
        )}
      </div>

      <div style={{ display: 'flex', gap: '12px', marginBottom: '16px' }}>
        <button
          type="button"
          className="btn-base"
          style={{
            backgroundColor: '#10b981',
            borderColor: '#059669',
            fontWeight: 600,
          }}
          onClick={handleStart}
          disabled={isRunning}
        >
          {isRunning ? (
            <span style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
              <LoadingSpinner /> 處理中...
            </span>
          ) : (
            zhTW.developer.autoOptimize
          )}
        </button>

        <button
          type="button"
          className="btn-base"
          style={{
            backgroundColor: '#f87171',
            borderColor: '#dc2626',
            fontWeight: 600,
          }}
          onClick={handleTerminate}
          disabled={!isRunning}
        >
          {zhTW.rescue.stop}
        </button>
      </div>

      {isRunning && (
        <div style={{ marginBottom: '16px' }}>
          <div style={{ marginBottom: '8px', color: '#94a3b8' }}>
            {progress?.message || zhTW.workflow.running}
          </div>
          <progress
            style={{ width: '100%', height: '8px' }}
            value={progress?.percent || 0}
            max="100"
            aria-label={zhTW.developer.autoOptimize}
          />
        </div>
      )}

      {logs.length > 0 && (
        <details>
          <summary
            style={{ cursor: 'pointer', color: '#94a3b8', marginBottom: '8px' }}
          >
            檢視詳細日誌
          </summary>
          <div
            ref={logContainerRef}
            className="log-display"
            style={{
              height: '200px',
              overflowY: 'auto',
              backgroundColor: '#1e293b',
              padding: '12px',
              borderRadius: '6px',
              color: '#f8fafc',
              fontFamily: 'monospace',
              fontSize: '13px',
            }}
            role="log"
            aria-live="polite"
          >
            {logs.map((log, index) => (
              <div
                key={`${index}-${log.slice(0, 15)}`}
                style={{
                  color: getLogColor(log),
                  marginBottom: '4px',
                  borderLeft: `2px solid ${getLogColor(log)}`,
                  paddingLeft: '8px',
                  whiteSpace: 'pre-wrap',
                }}
              >
                {log}
              </div>
            ))}
            {progress?.message && (
              <div style={{ color: '#34d399' }}>{progress.message}</div>
            )}
          </div>
        </details>
      )}
    </section>
  )
}
