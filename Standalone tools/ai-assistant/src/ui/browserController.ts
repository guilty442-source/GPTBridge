import { useCallback, useState } from 'react'

type ElectronBridge = {
  invoke: (channel: string, ...args: unknown[]) => Promise<unknown>
}

type SetMessage = (message: string) => void

type BrowserResult = {
  ok: boolean
  id?: string
  message?: string
}

export type BrowserController = {
  browserUrl: string
  setBrowserUrl: (value: string) => void
  browserSession: string | null
  openBrowser: () => Promise<void>
  navigateBrowser: () => Promise<void>
  closeBrowser: () => Promise<void>
}

function electronBridge(): ElectronBridge | undefined {
  return (window as any).electron as ElectronBridge | undefined
}

const DEFAULT_BROWSER_URL = 'https://www.google.com'
const BROWSER_SESSION_ID = 'ai-assistant-browser'

export function useBrowserController(setMessage: SetMessage): BrowserController {
  const [browserUrl, setBrowserUrl] = useState(DEFAULT_BROWSER_URL)
  const [browserSession, setBrowserSession] = useState<string | null>(null)

  const openBrowser = useCallback(async () => {
    const electron = electronBridge()
    if (!electron) {
      setMessage('瀏覽器 IPC 尚未就緒')
      return
    }
    const bounds = {
      x: 0,
      y: 120,
      width: window.innerWidth,
      height: Math.max(200, window.innerHeight - 120),
    }
    const result = (await electron.invoke('embedded-browser:create', {
      id: browserSession || BROWSER_SESSION_ID,
      ownerModule: 'ai-assistant',
      url: browserUrl,
      bounds,
    })) as BrowserResult
    if (result.ok) {
      setBrowserSession(result.id || BROWSER_SESSION_ID)
      setMessage('已開啟瀏覽器')
    } else {
      setMessage(result.message || '瀏覽器開啟失敗')
    }
  }, [browserSession, browserUrl, setMessage])

  const navigateBrowser = useCallback(async () => {
    const electron = electronBridge()
    if (!electron || !browserSession) {
      setMessage('請先開啟瀏覽器')
      return
    }
    const result = (await electron.invoke('embedded-browser:navigate', {
      id: browserSession,
      url: browserUrl,
    })) as BrowserResult
    setMessage(result.ok ? '瀏覽器已導航' : result.message || '導航失敗')
  }, [browserSession, browserUrl, setMessage])

  const closeBrowser = useCallback(async () => {
    const electron = electronBridge()
    if (!electron || !browserSession) return
    await electron.invoke('embedded-browser:close', { id: browserSession })
    setBrowserSession(null)
    setMessage('已關閉瀏覽器')
  }, [browserSession, setMessage])

  return {
    browserUrl,
    setBrowserUrl,
    browserSession,
    openBrowser,
    navigateBrowser,
    closeBrowser,
  }
}
