import { useCallback, useEffect, useRef, useState } from 'react'

type ElectronAPI = {
  invoke: (channel: string, ...args: unknown[]) => Promise<unknown>
}

function getElectron(): ElectronAPI | null {
  const api = (window as unknown as { electron?: ElectronAPI }).electron
  return api ?? null
}

export type BrowserSession = {
  id: string
  url: string
  ownerModule: string
}

export type EmbeddedBrowserState = {
  sessionId: string | null
  currentUrl: string
  loading: boolean
  error: string
}

const OWNER_MODULE = 'ai-collaboration'

export function useEmbeddedBrowser() {
  const [state, setState] = useState<EmbeddedBrowserState>({
    sessionId: null,
    currentUrl: '',
    loading: false,
    error: '',
  })
  const sessionRef = useRef<string | null>(null)

  const invoke = useCallback(async (channel: string, ...args: unknown[]) => {
    const electron = getElectron()
    if (!electron) throw new Error('Electron IPC bridge is unavailable')
    return electron.invoke(channel, ...args)
  }, [])

  const navigate = useCallback(
    async (rawUrl: string) => {
      const url = normalizeUrl(rawUrl)
      if (!url) {
        setState((prev) => ({ ...prev, error: '請輸入有效的網址' }))
        return
      }
      const existingId = sessionRef.current
      setState((prev) => ({ ...prev, loading: true, error: '', currentUrl: url }))
      try {
        if (existingId) {
          await invoke('embedded-browser:navigate', { id: existingId, url })
          await invoke('embedded-browser:show', { id: existingId })
        } else {
          const result = (await invoke('embedded-browser:create', {
            id: `${OWNER_MODULE}:browser`,
            ownerModule: OWNER_MODULE,
            url,
          })) as { ok: boolean; id?: string; message?: string }
          if (!result.ok) throw new Error(result.message || '無法建立瀏覽器')
          sessionRef.current = result.id || `${OWNER_MODULE}:browser`
        }
        setState((prev) => ({
          ...prev,
          sessionId: sessionRef.current,
          loading: false,
        }))
      } catch (error) {
        setState((prev) => ({
          ...prev,
          loading: false,
          error: error instanceof Error ? error.message : '瀏覽器操作失敗',
        }))
      }
    },
    [invoke]
  )

  const showBrowser = useCallback(async () => {
    const id = sessionRef.current
    if (!id) return
    try {
      await invoke('embedded-browser:show', { id })
    } catch {
      // best-effort
    }
  }, [invoke])

  const hideBrowser = useCallback(async () => {
    const id = sessionRef.current
    if (!id) return
    try {
      await invoke('embedded-browser:hide', { id })
    } catch {
      // best-effort
    }
  }, [invoke])

  const closeBrowser = useCallback(async () => {
    const id = sessionRef.current
    if (!id) return
    try {
      await invoke('embedded-browser:close', { id })
    } catch {
      // best-effort
    }
    sessionRef.current = null
    setState({ sessionId: null, currentUrl: '', loading: false, error: '' })
  }, [invoke])

  const getUrl = useCallback(async (): Promise<string | null> => {
    const id = sessionRef.current
    if (!id) return null
    try {
      const result = (await invoke('embedded-browser:url', { id })) as {
        ok: boolean
        url?: string
      }
      return result.ok ? result.url || null : null
    } catch {
      return null
    }
  }, [invoke])

  useEffect(() => {
    return () => {
      const id = sessionRef.current
      if (!id) return
      void invoke('embedded-browser:hide', { id }).catch(() => {})
    }
  }, [invoke])

  return {
    state,
    navigate,
    showBrowser,
    hideBrowser,
    closeBrowser,
    getUrl,
  }
}

function normalizeUrl(input: string): string {
  const trimmed = input.trim()
  if (!trimmed) return ''
  if (/^https?:\/\//i.test(trimmed)) return trimmed
  if (/^[\w-]+(\.[\w-]+)+/.test(trimmed)) return `https://${trimmed}`
  return ''
}
