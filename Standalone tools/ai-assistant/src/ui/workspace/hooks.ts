import { useCallback, useEffect, useRef, useState } from 'react'

/** Result frame pushed by the backend over `ipc_event`. */
export type CommandResult<T = unknown> = {
  event: string
  payload: T | undefined
  at: number
}

/** Subscribe to a backend `ipc_event` name; cleans up on unmount so a
 * page switch never leaks a subscription (memory-bounded contract). */
export function useIpcEvent(
  eventName: string,
  handler: (payload: unknown) => void,
) {
  const ref = useRef(handler)
  ref.current = handler
  useEffect(() => {
    const listener = (e: Event) => {
      const detail = (e as CustomEvent).detail as
        | { event?: string; payload?: unknown }
        | undefined
      if (detail?.event === eventName) ref.current(detail.payload)
    }
    window.addEventListener('ipc_event', listener)
    return () => window.removeEventListener('ipc_event', listener)
  }, [eventName])
}

/** Subscribe to every `ipc_event`; handler decides relevance. */
export function useIpcEvents(handler: (event: string, payload: unknown) => void) {
  const ref = useRef(handler)
  ref.current = handler
  useEffect(() => {
    const listener = (e: Event) => {
      const detail = (e as CustomEvent).detail as
        | { event?: string; payload?: unknown }
        | undefined
      if (detail?.event) ref.current(String(detail.event), detail.payload)
    }
    window.addEventListener('ipc_event', listener)
    return () => window.removeEventListener('ipc_event', listener)
  }, [])
}

export type QueryState<T> = {
  data: T | undefined
  loading: boolean
  error: string
  /** true when the backend answered but had nothing — honest empty. */
  empty: boolean
  refreshedAt: number
}

/**
 * Command query: sends `command` through the governed WS channel and
 * captures the matching `<command>_result` frame. Re-runs on `deps`
 * change; supports manual `refresh()`.
 */
export function useCommandQuery<T = unknown>(
  sendCommand: (command: string, payload?: unknown) => { ok: boolean; message?: string },
  command: string,
  payload: unknown = {},
  deps: unknown[] = [],
  isEmpty?: (data: T) => boolean,
): QueryState<T> & { refresh: () => void } {
  const [state, setState] = useState<QueryState<T>>({
    data: undefined,
    loading: true,
    error: '',
    empty: false,
    refreshedAt: 0,
  })
  const [nonce, setNonce] = useState(0)
  const payloadRef = useRef(payload)
  payloadRef.current = payload

  useIpcEvent(`${command}_result`, (raw) => {
    const p = raw as Record<string, unknown> | undefined
    setState({
      data: p as T,
      loading: false,
      error:
        p && p.ok === false
          ? String(p.error_code || p.message || 'BACKEND_ERROR')
          : '',
      empty: p != null && isEmpty ? isEmpty(p as T) : false,
      refreshedAt: Date.now(),
    })
  })

  const fire = useCallback(() => {
    setState((s) => ({ ...s, loading: true, error: '' }))
    const ack = sendCommand(command, payloadRef.current)
    if (!ack.ok) {
      setState((s) => ({
        ...s,
        loading: false,
        error: ack.message || '指令未送出',
      }))
    }
  }, [sendCommand, command])

  useEffect(() => {
    fire()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [command, nonce, ...deps])

  const refresh = useCallback(() => setNonce((n) => n + 1), [])
  return { ...state, refresh }
}
