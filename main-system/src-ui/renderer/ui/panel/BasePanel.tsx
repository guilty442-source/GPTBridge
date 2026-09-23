import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react'
import { PanelDrawer } from './PanelDrawer'
import { mainSystemLocale } from '@/locales/main-system'
import './BasePanel.css'

const t = mainSystemLocale.toolbox

interface BasePanelProps {
  open: boolean
  onClose: () => void
  sendCommand: (command: string, payload?: unknown) => { ok: boolean; queued: boolean; message?: string }
  waitForIpcEvent: (
    eventName: string,
    timeoutMs: number,
    predicate?: (payload: Record<string, unknown>) => boolean
  ) => Promise<Record<string, unknown>>
  backendSocket: { status: string }
}

interface BasePanelState {
  loading: boolean
  error: string
  connected: boolean
}

interface BasePanelContextValue {
  state: BasePanelState
  locale: {
    launch: string
    launching: string
    stop: string
    closing: string
    status: string
    statusStopped: string
    statusRunning: string
    statusError: string
    errorFetch: string
    errorTimeout: string
    disconnected: string
    refresh: string
    loading: string
  }
  send: (command: string, payload?: Record<string, unknown>) => { ok: boolean; queued: boolean; message?: string }
  waitForEvent: (eventName: string, timeoutMs?: number, predicate?: (payload: Record<string, unknown>) => boolean) => Promise<Record<string, unknown>>
  generateRequestId: (prefix?: string) => string
  setLoading: (loading: boolean) => void
  setError: (error: string) => void
  clearError: () => void
  connected: boolean
  loading: boolean
}

interface BasePanelPropsExtended extends BasePanelProps {
  title: string
  eyebrow?: string
  icon?: string
  children: (ctx: BasePanelContextValue) => ReactNode
  headerActions?: ReactNode
  side?: 'right' | 'bottom'
}

const DEFAULT_TIMEOUT_MS = 30000

export function BasePanel({
  open,
  onClose,
  sendCommand,
  waitForIpcEvent,
  backendSocket,
  children,
  title,
  eyebrow,
  icon,
  headerActions,
  side = 'right',
}: BasePanelPropsExtended) {
  const [state, setState] = useState<BasePanelState>({
    loading: false,
    error: '',
    connected: backendSocket.status === 'Connected',
  })
  const mountedRef = useRef(true)

  // Update connection status
  useEffect(() => {
    setState(prev => ({ ...prev, connected: backendSocket.status === 'Connected' }))
  }, [backendSocket.status])

  // Cleanup on unmount
  useEffect(() => {
    mountedRef.current = true
    return () => { mountedRef.current = false }
  }, [])

  const setLoading = useCallback((loading: boolean) => {
    setState(prev => ({ ...prev, loading }))
  }, [])

  const setError = useCallback((error: string) => {
    setState(prev => ({ ...prev, error }))
  }, [])

  const clearError = useCallback(() => {
    setState(prev => ({ ...prev, error: '' }))
  }, [])

  const send = useCallback((command: string, payload?: Record<string, unknown>) => {
    return sendCommand(command, payload)
  }, [sendCommand])

  const waitForEvent = useCallback(async (
    eventName: string,
    timeoutMs = 30000,
    predicate?: (payload: Record<string, unknown>) => boolean
  ) => {
    return waitForIpcEvent(eventName, timeoutMs, predicate)
  }, [waitForIpcEvent])

  const generateRequestId = useCallback((prefix = 'panel') => {
    return `${prefix}:${Date.now()}:${Math.random().toString(16).slice(2)}`
  }, [])

  // Extract locale strings for common actions
  const locale = {
    launch: mainSystemLocale.toolbox.start,
    launching: mainSystemLocale.toolbox.starting,
    stop: mainSystemLocale.toolbox.stop,
    closing: mainSystemLocale.toolbox.stopping,
    status: mainSystemLocale.toolbox.status,
    statusStopped: mainSystemLocale.toolbox.statusStopped,
    statusRunning: mainSystemLocale.toolbox.statusRunning,
    statusError: mainSystemLocale.toolbox.statusError,
    errorFetch: mainSystemLocale.toolbox.errorFetch,
    errorTimeout: mainSystemLocale.toolbox.errorTimeout,
    disconnected: mainSystemLocale.toolbox.disconnected,
    refresh: mainSystemLocale.toolbox.refresh,
    loading: mainSystemLocale.toolbox.loading,
  }

  return (
    <PanelDrawer
      open={open}
      onClose={onClose}
      title={title}
      eyebrow={eyebrow}
      icon={icon}
      side={side}
      headerActions={headerActions}
    >
      <div className="base-panel">
        {state.error && (
          <div className="base-panel__error" role="alert">
            {state.error}
            <button type="button" className="base-panel__error-dismiss" onClick={clearError} aria-label="關閉">
              <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
                <path d="M4 4L10 10M10 4L4 10" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
              </svg>
            </button>
          </div>
        )}

        {!state.connected && (
          <div className="base-panel__disconnected">
            {locale.disconnected}
          </div>
        )}

        {children({
          state,
          locale,
          send,
          waitForEvent,
          generateRequestId,
          setLoading,
          setError,
          clearError,
          connected: state.connected,
          loading: state.loading,
        })}
      </div>
    </PanelDrawer>
  )
}

export { type BasePanelProps, type BasePanelState, type BasePanelContextValue }