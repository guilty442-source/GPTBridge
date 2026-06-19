import { zhTW } from '@/i18n/zhTW'
import React, { useEffect, useState } from 'react'
import { LoadingSpinner } from './LoadingSpinner'

type DiscussionPanelProps = {
  sendCommand: (command: string, payload?: Record<string, unknown>) => void
}

export const DiscussionPanel: React.FC<DiscussionPanelProps> = ({
  sendCommand,
}) => {
  const [promptText, setPromptText] = useState('')
  const [selectedMode, setSelectedMode] = useState('mutual_review')
  const [isLoading, setIsLoading] = useState(false)

  useEffect(() => {
    const handleIpcEvent = (e: Event) => {
      const customEvent = e as CustomEvent
      const { event } = customEvent.detail || {}
      if (event === 'discussion_result') {
        setIsLoading(false)
      }
    }
    window.addEventListener('ipc_event', handleIpcEvent)
    return () => window.removeEventListener('ipc_event', handleIpcEvent)
  }, [])

  const applyTemplate = (text: string) => {
    setPromptText((current) => (current ? `${current}\n\n${text}` : text))
  }

  return (
    <section className="section discussion-panel">
      <div className="summary-header">
        <h2>{zhTW.design.consensus}</h2>
      </div>
      <div className="button-group">
        <button
          type="button"
          className="btn-base"
          onClick={() => applyTemplate(zhTW.design.templateText.bugFix)}
        >
          {zhTW.design.templates.bugFix}
        </button>
        <button
          type="button"
          className="btn-base"
          onClick={() => applyTemplate(zhTW.design.templateText.optimize)}
        >
          {zhTW.design.templates.optimize}
        </button>
        <button
          type="button"
          className="btn-base"
          onClick={() => applyTemplate(zhTW.design.templateText.createTool)}
        >
          {zhTW.design.templates.createTool}
        </button>
        <button
          type="button"
          className="btn-base"
          onClick={() => applyTemplate(zhTW.design.templateText.refactor)}
        >
          {zhTW.design.templates.refactor}
        </button>
      </div>
      <div>
        <textarea
          className="textarea"
          value={promptText}
          onChange={(event) => setPromptText(event.target.value)}
          placeholder={zhTW.design.promptPlaceholder}
          rows={10}
        />
      </div>
      <div style={{ display: 'flex', alignItems: 'center', gap: '12px' }}>
        <select
          id="discussion-mode"
          className="btn-base"
          value={selectedMode}
          onChange={(event) => setSelectedMode(event.target.value)}
          style={{ flex: 1 }}
        >
          <option value="mutual_review">{zhTW.design.mutualReview}</option>
          <option value="chatgpt_first">{zhTW.design.chatgptFirst}</option>
          <option value="gemini_first">{zhTW.design.geminiFirst}</option>
          <option value="ask_both">{zhTW.design.askBoth}</option>
        </select>
        <button
          type="button"
          className="btn-base"
          style={{
            backgroundColor: '#10b981',
            borderColor: '#059669',
            fontWeight: 600,
            padding: '8px 24px',
          }}
          disabled={isLoading}
          onClick={() => {
            setIsLoading(true)
            sendCommand('discussion_query', {
              text: promptText,
              mode: selectedMode,
            })
          }}
        >
          {isLoading ? (
            <span style={{ display: 'flex', alignItems: 'center', gap: '6px' }}>
              <LoadingSpinner /> 處理中...
            </span>
          ) : (
            zhTW.design.getConsensus
          )}
        </button>
      </div>
    </section>
  )
}
