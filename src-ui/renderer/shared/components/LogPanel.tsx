import { zhTW } from '@/i18n/zhTW'
import React, { useEffect, useRef } from 'react'

type LogPanelProps = {
  log: string[]
  title?: string
  emptyText?: string
}

export const LogPanel: React.FC<LogPanelProps> = ({
  log,
  title = zhTW.settings.coreLog,
  emptyText = zhTW.settings.noCoreLog,
}) => {
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
  }, [log])

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
    return '#f9fafb'
  }

  return (
    <section className="section">
      <h2 style={{ fontSize: '16px', marginBottom: '16px', marginTop: 0 }}>
        {title}
      </h2>
      <div
        ref={logContainerRef}
        className="log-container"
        role="log"
        aria-live="polite"
      >
        {log.length > 0 ? (
          log.map((line, index) => (
            <div
              key={`${index}-${line.slice(0, 20)}`}
              style={{
                color: getLogColor(line),
                marginBottom: '4px',
                borderLeft: `2px solid ${getLogColor(line)}`,
                paddingLeft: '8px',
                whiteSpace: 'pre-wrap',
              }}
            >
              {line}
            </div>
          ))
        ) : (
          <div style={{ color: '#64748b' }}>{emptyText}</div>
        )}
      </div>
    </section>
  )
}
