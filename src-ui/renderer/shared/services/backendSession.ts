type BackendSessionDescriptor = {
  token?: unknown
  websocketUrl?: unknown
}

const EXPECTED_URL_PREFIX = 'ws://127.0.0.1:8765/'

export async function getAuthenticatedBackendWebSocketUrl(): Promise<string> {
  const api = window.electron
  if (!api?.invoke) {
    throw new Error('主程式連線介面尚未就緒。')
  }
  const raw = (await api.invoke(
    'app:get-backend-session'
  )) as BackendSessionDescriptor | null
  const websocketUrl = String(raw?.websocketUrl || '').trim()
  const token = String(raw?.token || '').trim()
  if (!websocketUrl.startsWith(EXPECTED_URL_PREFIX) || token.length < 32) {
    throw new Error('後端驗證資訊無效，無法建立安全連線。')
  }
  return websocketUrl
}
