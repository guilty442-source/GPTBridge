type BackendSessionDescriptor = {
  websocketUrl?: unknown
}

const LOOPBACK_HOST = '127.0.0.1'

function isValidLoopbackWebSocketUrl(value: string): boolean {
  let parsed: URL
  try {
    parsed = new URL(value)
  } catch {
    return false
  }
  if (parsed.protocol !== 'ws:' || parsed.hostname !== LOOPBACK_HOST) return false
  const port = Number(parsed.port)
  return Number.isInteger(port) && port > 0 && port <= 65535
}

export async function getAuthenticatedBackendWebSocketUrl(): Promise<string> {
  const api = window.electron
  if (!api?.invoke) {
    throw new Error('主程式連線介面尚未就緒。')
  }
  const raw = (await api.invoke(
    'app:get-backend-session'
  )) as BackendSessionDescriptor | null
  const websocketUrl = String(raw?.websocketUrl || '').trim()
  if (!isValidLoopbackWebSocketUrl(websocketUrl)) {
    throw new Error('後端驗證資訊無效，無法建立安全連線。')
  }
  return websocketUrl
}
