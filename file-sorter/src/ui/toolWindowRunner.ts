import { useCallback, useRef, type CSSProperties } from 'react'
import { useLocalBackendSocket } from '../../../shared-layer/src/ui/toolWindow/useLocalBackendSocket'
import { waitForIpcEvent } from '../../../shared-layer/src/ui/toolWindow/toolWindowUtils'
import type { ToolRunOptions, ToolRunResult, OpenPathResult } from '../../../shared-layer/src/ui/toolWindow/toolWindowUtils'

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

const RUN_CANCELLATION_GRACE_MS = 10_000
const RESULT_DELIVERY_GRACE_MS = 5_000
const OFFLINE_QUEUE_SAFE_FLAGS = new Set([
  '--cleanup-scan',
  '--history-json',
  '--list-folders',
  '--list-keywords',
  '--list-source-files',
  '--preview-json',
  '--profiles-json',
])

const OFFLINE_QUEUE_MUTATION_FLAGS = new Set([
  '--apply-plan',
  '--delete-keyword',
  '--folder',
  '--new-keyword',
  '--remove-keyword',
  '--set-profile-enabled',
  '--undo-last',
  '--update-keyword',
  '--upsert-keyword',
])

function isKnownReadOnlyToolRun(args: string[]): boolean {
  return (
    args.some((arg) => OFFLINE_QUEUE_SAFE_FLAGS.has(arg)) &&
    !args.some((arg) => OFFLINE_QUEUE_MUTATION_FLAGS.has(arg))
  )
}

export function useToolRunner(toolId: string, timeoutMs = 120000) {
  const {
    removeQueuedCommand,
    sendCommand,
    status: socketStatus,
  } = useLocalBackendSocket()
  const queueRef = useRef<Promise<void>>(Promise.resolve())
  const activeRequestIdRef = useRef('')
  const activeAbortRef = useRef<AbortController | null>(null)
  const activeQueueIdRef = useRef('')

  const requestToolRun = useCallback(
    async (
      args: string[],
      options: ToolRunOptions = {}
    ): Promise<ToolRunResult> => {
      const runRequest = async (): Promise<ToolRunResult> => {
        const requestId = `${toolId}:${Date.now()}:${Math.random()
          .toString(16)
          .slice(2)}`
        if (options.signal?.aborted) {
          return {
            ok: false,
            cancelled: true,
            tool_id: toolId,
            request_id: requestId,
            message: '請求在送出前已撤銷。',
          }
        }
        activeRequestIdRef.current = requestId
        options.onRequestId?.(requestId)
        const requestTimeoutMs = Math.max(1, options.timeoutMs || timeoutMs)
        const abortController = new AbortController()
        let cancellationGraceTimer: number | null = null
        activeAbortRef.current = abortController
        const handleExternalAbort = () => {
          const queueId = activeQueueIdRef.current
          if (queueId && removeQueuedCommand(queueId)) {
            abortController.abort(new Error('請求已撤銷。'))
            return
          }
          const cancellation = sendCommand('toolbox_cancel_tool_run', {
            tool_id: toolId,
            source: 'tool_window',
            request_id: requestId,
          })
          if (!cancellation.ok && !cancellation.queued) {
            abortController.abort(
              new Error(cancellation.message || '無法撤銷工具請求。')
            )
            return
          }
          cancellationGraceTimer = window.setTimeout(() => {
            abortController.abort(new Error('等待工具停止逾時。'))
          }, RUN_CANCELLATION_GRACE_MS)
        }
        options.signal?.addEventListener('abort', handleExternalAbort, { once: true })
        const resultPromise = waitForIpcEvent<ToolRunResult>(
          'toolbox_run_tool_result',
          requestTimeoutMs,
          (payload) =>
            String(payload.tool_id || '') === toolId &&
            String(payload.request_id || '') === requestId,
          abortController.signal
        )
        const allowOfflineQueue =
          options.mode === 'read-only' && isKnownReadOnlyToolRun(args)
        const sent = sendCommand('toolbox_run_tool', {
          tool_id: toolId,
          args,
          source: 'tool_window',
          request_id: requestId,
          timeout_seconds: Math.max(
            1,
            Math.floor((requestTimeoutMs - RESULT_DELIVERY_GRACE_MS) / 1000)
          ),
        }, {
          allowOfflineQueue,
          queueTtlMs: options.queueTtlMs,
          onQueueExpired: (error) => abortController.abort(error),
        })
        activeQueueIdRef.current = sent.queueId || ''
        if (!sent.ok && !sent.queued) {
          const failure = {
            ok: false,
            tool_id: toolId,
            request_id: requestId,
            message: sent.message || '後端尚未接收工具指令',
          }
          abortController.abort(new Error(failure.message))
          try {
            await resultPromise
          } catch {
            // The waiter was intentionally revoked because nothing was sent.
          }
          options.signal?.removeEventListener('abort', handleExternalAbort)
          activeAbortRef.current = null
          activeQueueIdRef.current = ''
          if (activeRequestIdRef.current === requestId) {
            activeRequestIdRef.current = ''
          }
          return failure
        }

        try {
          return await resultPromise
        } catch (error) {
          if (sent.queueId) removeQueuedCommand(sent.queueId)
          throw error
        } finally {
          if (cancellationGraceTimer !== null) {
            window.clearTimeout(cancellationGraceTimer)
          }
          options.signal?.removeEventListener('abort', handleExternalAbort)
          activeAbortRef.current = null
          activeQueueIdRef.current = ''
          if (activeRequestIdRef.current === requestId) {
            activeRequestIdRef.current = ''
          }
        }
      }

      const queued = queueRef.current.then(runRequest, runRequest)
      queueRef.current = queued.then(
        () => undefined,
        () => undefined
      )
      return queued
    },
    [removeQueuedCommand, sendCommand, timeoutMs, toolId]
  )

  const cancelToolRun = useCallback(async (requestId?: string) => {
    const targetRequestId = requestId || activeRequestIdRef.current
    if (!targetRequestId) {
      return { ok: false, message: 'There is no active File Sorter request.' }
    }
    const queuedId = activeQueueIdRef.current
    if (queuedId && removeQueuedCommand(queuedId)) {
      activeAbortRef.current?.abort(new Error('Queued tool run was cancelled.'))
      return {
        ok: true,
        cancelled: true,
        tool_id: toolId,
        request_id: targetRequestId,
        message: 'Queued read-only command cancelled.',
      }
    }
    const abortController = new AbortController()
    const resultPromise = waitForIpcEvent<OpenPathResult>(
      'toolbox_cancel_tool_run_result',
      10000,
      (payload) =>
        (!payload.tool_id || String(payload.tool_id) === toolId) &&
        String(payload.request_id || '') === targetRequestId,
      abortController.signal
    )
    const sent = sendCommand('toolbox_cancel_tool_run', {
      tool_id: toolId,
      source: 'tool_window',
      request_id: targetRequestId,
    })
    if (!sent.ok && !sent.queued) {
      const failure = {
        ok: false,
        message: sent.message || '後端尚未接收停止指令',
      }
      abortController.abort(new Error(failure.message))
      try {
        await resultPromise
      } catch {
        // The waiter was intentionally revoked because nothing was sent.
      }
      return failure
    }
    return await resultPromise
  }, [removeQueuedCommand, sendCommand, toolId])

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
    background:
      'radial-gradient(circle at 10% 0%, rgba(14, 165, 233, 0.14), transparent 34%), linear-gradient(180deg, #07101d 0%, #0b1220 54%, #070b12 100%)',
    color: '#f8fafc',
    fontFamily: '"Noto Sans TC", "Segoe UI", sans-serif',
    padding: '32px clamp(18px, 4vw, 52px) 48px',
    boxSizing: 'border-box',
  },
  card: {
    maxWidth: '1180px',
    margin: '0 auto',
    background: 'rgba(8, 15, 27, 0.82)',
    border: '1px solid rgba(71, 85, 105, 0.55)',
    borderRadius: '20px',
    padding: 'clamp(20px, 3vw, 34px)',
    boxShadow: '0 28px 80px rgba(0, 0, 0, 0.46)',
    backdropFilter: 'blur(18px)',
  },
  header: {
    display: 'flex',
    justifyContent: 'space-between',
    gap: '16px',
    alignItems: 'flex-start',
    marginBottom: '18px',
  },
  kicker: {
    color: '#38bdf8',
    fontSize: '12px',
    fontWeight: 800,
    letterSpacing: '0.08em',
    textTransform: 'uppercase',
    marginBottom: '6px',
  },
  title: {
    margin: 0,
    fontSize: 'clamp(26px, 4vw, 40px)',
    lineHeight: 1.25,
  },
  muted: {
    margin: '8px 0 0',
    color: '#94a3b8',
    fontSize: '14px',
    lineHeight: 1.6,
  },
  badge: {
    border: '1px solid rgba(56, 189, 248, 0.42)',
    borderRadius: '999px',
    color: '#bae6fd',
    background: 'rgba(14, 165, 233, 0.10)',
    padding: '7px 12px',
    fontSize: '12px',
    fontWeight: 700,
    display: 'inline-flex',
    alignItems: 'center',
    gap: '7px',
  },
  badgeOnline: {
    color: '#bbf7d0',
    borderColor: 'rgba(52, 211, 153, 0.38)',
    background: 'rgba(16, 185, 129, 0.1)',
  },
  badgeDot: {
    width: '7px',
    height: '7px',
    borderRadius: '999px',
    background: 'currentColor',
    boxShadow: '0 0 12px currentColor',
  },
  workspaceHero: {
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'space-between',
    gap: '20px',
    padding: '22px',
    borderRadius: '16px',
    border: '1px solid rgba(56, 189, 248, 0.28)',
    background: 'linear-gradient(135deg, rgba(14, 165, 233, 0.16), rgba(15, 23, 42, 0.72))',
    boxShadow: 'inset 0 1px 0 rgba(255,255,255,0.04)',
  },
  workspaceCopy: {
    display: 'grid',
    gap: '5px',
    minWidth: 0,
  },
  workspaceTitle: {
    color: '#f8fafc',
    fontSize: '18px',
  },
  workspacePath: {
    color: '#94a3b8',
    fontSize: '13px',
    overflow: 'hidden',
    textOverflow: 'ellipsis',
    whiteSpace: 'nowrap',
  },
  eyebrow: {
    color: '#38bdf8',
    fontSize: '10px',
    fontWeight: 900,
    letterSpacing: '0.14em',
  },
  sectionHeading: {
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'space-between',
    gap: '16px',
    marginBottom: '12px',
  },
  sectionTitle: {
    display: 'block',
    marginTop: '3px',
    color: '#f8fafc',
    fontSize: '17px',
  },
  alwaysOnBadge: {
    borderRadius: '999px',
    padding: '6px 10px',
    background: 'rgba(52, 211, 153, 0.12)',
    color: '#86efac',
    fontSize: '11px',
    fontWeight: 800,
  },
  automationStatus: {
    display: 'flex',
    alignItems: 'flex-start',
    gap: '10px',
    marginTop: '14px',
    padding: '12px 14px',
    borderRadius: '10px',
    background: 'rgba(2, 6, 23, 0.42)',
    border: '1px solid rgba(51, 65, 85, 0.55)',
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
    background: 'linear-gradient(145deg, rgba(15, 23, 42, 0.95), rgba(8, 16, 30, 0.95))',
    border: '1px solid rgba(71, 85, 105, 0.46)',
    borderRadius: '14px',
    color: '#cbd5e1',
    padding: '18px',
    lineHeight: 1.6,
    marginBottom: '18px',
    boxShadow: '0 12px 30px rgba(0, 0, 0, 0.18)',
  },
  heroStats: {
    display: 'grid',
    gridTemplateColumns: 'repeat(auto-fit, minmax(190px, 1fr))',
    gap: '10px',
    margin: '16px 0 18px',
  },
  statCard: {
    background: 'rgba(15, 23, 42, 0.78)',
    border: '1px solid rgba(51, 65, 85, 0.75)',
    borderRadius: '12px',
    padding: '14px 16px',
    display: 'grid',
    gap: '5px',
    color: '#94a3b8',
    fontSize: '12px',
  },
  navBar: {
    display: 'flex',
    flexWrap: 'wrap',
    gap: '8px',
    position: 'sticky',
    top: '10px',
    zIndex: 5,
    padding: '10px',
    marginBottom: '18px',
    border: '1px solid rgba(51, 65, 85, 0.72)',
    borderRadius: '12px',
    background: 'rgba(7, 16, 29, 0.90)',
    backdropFilter: 'blur(14px)',
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
