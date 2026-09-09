import { useCallback, useRef, type CSSProperties } from 'react'
import { useLocalBackendSocket } from '../../../shared-layer/src/ui/toolWindow/useLocalBackendSocket'
import { waitForIpcEvent } from '../../../shared-layer/src/ui/toolWindow/toolWindowUtils'
import type { ToolRunResult, OpenPathResult } from '../../../shared-layer/src/ui/toolWindow/toolWindowUtils'

export {
  formatFileSize,
  formatRunOutput,
  isAbsoluteFilesystemPath,
  openPath,
  parseToolJson,
  selectFolder,
  type OpenPathResult,
  type ToolRunMode,
  type ToolRunOptions,
  type ToolRunResult,
} from '../../../shared-layer/src/ui/toolWindow/toolWindowUtils'

export function useToolRunner(toolId: string, timeoutMs = 120000) {
  const { sendCommand, status: socketStatus } = useLocalBackendSocket()
  const queueRef = useRef<Promise<void>>(Promise.resolve())

  const requestToolRun = useCallback(
    async (args: string[]) => {
      const runRequest = async (): Promise<ToolRunResult> => {
        const requestId = `${toolId}:${Date.now()}:${Math.random()
          .toString(16)
          .slice(2)}`
        const resultPromise = waitForIpcEvent<ToolRunResult>(
          'toolbox_run_tool_result',
          timeoutMs,
          (payload) =>
            String(payload.tool_id || '') === toolId &&
            String(payload.request_id || '') === requestId
        )
        const sent = sendCommand('toolbox_run_tool', {
          tool_id: toolId,
          args,
          source: 'tool_window',
          request_id: requestId,
        })
        if (!sent.ok && !sent.queued) {
          return {
            ok: false,
            tool_id: toolId,
            request_id: requestId,
            message: sent.message || '後端尚未接收工具指令',
          }
        }
        return await resultPromise
      }

      const queued = queueRef.current.then(runRequest, runRequest)
      queueRef.current = queued.then(
        () => undefined,
        () => undefined
      )
      return queued
    },
    [sendCommand, timeoutMs, toolId]
  )

  const cancelToolRun = useCallback(async () => {
    const resultPromise = waitForIpcEvent<OpenPathResult>(
      'toolbox_cancel_tool_run_result',
      10000,
      (payload) => !payload.tool_id || String(payload.tool_id) === toolId
    )
    const sent = sendCommand('toolbox_cancel_tool_run', {
      tool_id: toolId,
      source: 'tool_window',
    })
    if (!sent.ok && !sent.queued) {
      return { ok: false, message: sent.message || '後端尚未接收停止指令' }
    }
    return await resultPromise
  }, [sendCommand, toolId])

  return {
    cancelToolRun,
    requestToolRun,
    sendCommand,
    socketStatus,
  }
}

export const toolWindowStyles: Record<string, CSSProperties> = {
  app: {
    minHeight: '100vh',
    background: '#0b0f17',
    color: '#f8fafc',
    fontFamily: '"Noto Sans TC", "Segoe UI", sans-serif',
    padding: '28px',
    boxSizing: 'border-box',
  },
  card: {
    maxWidth: '980px',
    margin: '0 auto',
    background: '#111827',
    border: '1px solid #243044',
    borderRadius: '10px',
    padding: '22px',
    boxShadow: '0 22px 54px rgba(0, 0, 0, 0.42)',
  },
  header: {
    display: 'flex',
    justifyContent: 'space-between',
    gap: '16px',
    alignItems: 'flex-start',
    marginBottom: '22px',
  },
  kicker: {
    color: '#7dd3fc',
    fontSize: '12px',
    fontWeight: 800,
    letterSpacing: '0.08em',
    textTransform: 'uppercase',
    marginBottom: '6px',
  },
  title: {
    margin: 0,
    fontSize: '24px',
    lineHeight: 1.25,
  },
  muted: {
    margin: '8px 0 0',
    color: '#94a3b8',
    fontSize: '14px',
    lineHeight: 1.6,
  },
  badge: {
    border: '1px solid #334155',
    borderRadius: '999px',
    color: '#cbd5e1',
    padding: '5px 10px',
    fontSize: '12px',
    fontWeight: 700,
  },
  fieldGroup: {
    marginBottom: '16px',
  },
  label: {
    display: 'block',
    color: '#cbd5e1',
    fontSize: '13px',
    fontWeight: 700,
    marginBottom: '8px',
  },
  inlineRow: {
    display: 'flex',
    gap: '10px',
  },
  input: {
    flex: 1,
    minWidth: 0,
    background: '#0b1220',
    border: '1px solid #334155',
    borderRadius: '8px',
    color: '#f8fafc',
    padding: '11px 12px',
    fontSize: '14px',
    outline: 'none',
  },
  textarea: {
    width: '100%',
    minHeight: '76px',
    boxSizing: 'border-box',
    background: '#0b1220',
    border: '1px solid #334155',
    borderRadius: '8px',
    color: '#f8fafc',
    padding: '11px 12px',
    fontSize: '14px',
    outline: 'none',
    resize: 'vertical',
  },
  notice: {
    background: '#0b1220',
    border: '1px solid #1e293b',
    borderRadius: '8px',
    color: '#cbd5e1',
    padding: '12px',
    lineHeight: 1.6,
    marginBottom: '16px',
  },
  checkboxRow: {
    display: 'flex',
    alignItems: 'center',
    gap: '9px',
    color: '#e2e8f0',
    cursor: 'pointer',
  },
  noticeText: {
    color: '#94a3b8',
    fontSize: '12px',
    lineHeight: 1.6,
    margin: '8px 0 0',
  },
  actions: {
    display: 'flex',
    flexWrap: 'wrap',
    gap: '10px',
    justifyContent: 'flex-end',
    marginTop: '18px',
  },
  primaryButton: {
    border: 0,
    borderRadius: '8px',
    background: '#7dd3fc',
    color: '#082f49',
    padding: '11px 16px',
    fontWeight: 900,
    cursor: 'pointer',
  },
  secondaryButton: {
    border: '1px solid #334155',
    borderRadius: '8px',
    background: '#172033',
    color: '#e2e8f0',
    padding: '10px 13px',
    fontWeight: 800,
    cursor: 'pointer',
  },
  dangerButton: {
    border: '1px solid #b91c1c',
    borderRadius: '8px',
    background: '#7f1d1d',
    color: '#fee2e2',
    padding: '10px 13px',
    fontWeight: 900,
    cursor: 'pointer',
  },
  statusPanel: {
    marginTop: '18px',
    border: '1px solid #243044',
    borderRadius: '8px',
    background: '#0b1220',
    padding: '14px',
  },
  statusLine: {
    display: 'flex',
    alignItems: 'center',
    gap: '10px',
    marginBottom: '10px',
  },
  statusDot: {
    width: '10px',
    height: '10px',
    borderRadius: '999px',
  },
  output: {
    maxHeight: '260px',
    overflow: 'auto',
    whiteSpace: 'pre-wrap',
    wordBreak: 'break-word',
    margin: 0,
    color: '#cbd5e1',
    fontSize: '12px',
    lineHeight: 1.5,
  },
  resultPanel: {
    border: '1px solid #243044',
    borderRadius: '8px',
    background: '#0b1220',
    padding: '14px',
    marginTop: '14px',
  },
  resultList: {
    display: 'grid',
    gap: '8px',
    marginTop: '10px',
    maxHeight: '360px',
    overflow: 'auto',
  },
  resultRow: {
    display: 'flex',
    alignItems: 'center',
    gap: '10px',
    border: '1px solid #1e293b',
    borderRadius: '8px',
    background: '#111827',
    padding: '10px',
  },
  resultText: {
    flex: 1,
    minWidth: 0,
  },
  resultPath: {
    display: 'block',
    color: '#f8fafc',
    fontWeight: 800,
    overflow: 'hidden',
    textOverflow: 'ellipsis',
    whiteSpace: 'nowrap',
  },
  resultMeta: {
    display: 'block',
    color: '#94a3b8',
    fontSize: '12px',
    lineHeight: 1.5,
  },
  sliderGrid: {
    display: 'grid',
    gridTemplateColumns: 'repeat(auto-fit, minmax(210px, 1fr))',
    gap: '12px',
    marginTop: '14px',
  },
  sliderControl: {
    display: 'grid',
    gap: '7px',
    background: '#08101d',
    border: '1px solid #243044',
    borderRadius: '8px',
    padding: '10px',
  },
  sliderHeader: {
    display: 'flex',
    justifyContent: 'space-between',
    gap: '10px',
    color: '#e2e8f0',
    fontSize: '12px',
    fontWeight: 800,
  },
  rangeInput: {
    width: '100%',
    minWidth: 0,
  },
  sliderMeta: {
    display: 'flex',
    justifyContent: 'space-between',
    gap: '10px',
    color: '#94a3b8',
    fontSize: '11px',
  },
}