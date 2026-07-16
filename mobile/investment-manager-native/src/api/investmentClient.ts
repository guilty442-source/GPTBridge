import type {
  InvestmentMobileSnapshot,
  MobilePlatformContract,
  NativeClient,
} from './contracts'

const REQUEST_TIMEOUT_MS = 12_000

function isPrivateHost(hostname: string): boolean {
  const host = hostname.toLowerCase()
  if (
    host === 'localhost' ||
    host === '::1' ||
    host.endsWith('.local') ||
    host.startsWith('127.') ||
    host.startsWith('10.') ||
    host.startsWith('192.168.')
  ) {
    return true
  }
  const match = /^172\.(\d{1,3})\./.exec(host)
  const secondOctet = match ? Number(match[1]) : -1
  return secondOctet >= 16 && secondOctet <= 31
}

export function normalizeBaseUrl(value: string): string {
  const raw = value.trim().replace(/\/+$/, '')
  if (!raw) throw new Error('請輸入桌面端顯示的手機連線網址。')

  let parsed: URL
  try {
    parsed = new URL(raw)
  } catch {
    throw new Error('連線網址格式不正確。')
  }
  if (parsed.username || parsed.password || parsed.search || parsed.hash) {
    throw new Error('連線網址不可包含帳密、查詢參數或片段。')
  }
  if (parsed.protocol === 'https:') return raw
  if (parsed.protocol === 'http:' && isPrivateHost(parsed.hostname)) return raw
  throw new Error('外部網路必須使用 HTTPS；HTTP 僅允許本機或私人區網。')
}

async function requestJson<T>(
  baseUrl: string,
  path: string,
  init: RequestInit = {},
  sessionToken = ''
): Promise<T> {
  const controller = new AbortController()
  const timeout = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS)
  try {
    const response = await fetch(`${normalizeBaseUrl(baseUrl)}${path}`, {
      ...init,
      signal: controller.signal,
      headers: {
        Accept: 'application/json',
        ...(init.body ? { 'Content-Type': 'application/json' } : {}),
        ...(sessionToken
          ? { Authorization: `Bearer ${sessionToken}` }
          : {}),
        ...init.headers,
      },
    })
    const payload = (await response.json()) as {
      ok?: boolean
      message?: string
    }
    if (!response.ok || payload.ok === false) {
      throw new Error(
        payload.message ||
          (response.status === 403
            ? '配對工作階段已失效，請重新配對。'
            : `連線失敗（${response.status}）`)
      )
    }
    return payload as T
  } catch (error) {
    if (error instanceof Error && error.name === 'AbortError') {
      throw new Error('連線逾時，請確認手機與桌面服務可互相連線。')
    }
    throw error
  } finally {
    clearTimeout(timeout)
  }
}

export async function getPlatformContract(
  baseUrl: string
): Promise<MobilePlatformContract> {
  const result = await requestJson<{
    ok: true
    platform: MobilePlatformContract
  }>(baseUrl, '/api/platform')
  if (
    result.platform.contract_version !== 1 ||
    !result.platform.clients.includes('android_native')
  ) {
    throw new Error('桌面端手機共用契約版本不相容，請先升級桌面程式。')
  }
  return result.platform
}

export async function pairNativeDevice(
  baseUrl: string,
  pairingCode: string
): Promise<string> {
  const clientPlatform: NativeClient = 'android_native'
  const result = await requestJson<{
    ok: true
    session_token: string
  }>(baseUrl, '/api/pair', {
    method: 'POST',
    body: JSON.stringify({
      code: pairingCode.trim().toUpperCase(),
      client_platform: clientPlatform,
      device_name: 'AI投資管家 Android',
    }),
  })
  if (!result.session_token) throw new Error('桌面端沒有回傳配對憑證。')
  return result.session_token
}

export function getInvestmentSnapshot(
  baseUrl: string,
  sessionToken: string
): Promise<InvestmentMobileSnapshot> {
  return requestJson<InvestmentMobileSnapshot>(
    baseUrl,
    '/api/state',
    {},
    sessionToken
  )
}

export async function queueLocalAiCommand(
  baseUrl: string,
  sessionToken: string,
  instruction: string
): Promise<string> {
  const result = await requestJson<{ ok: true; message?: string }>(
    baseUrl,
    '/api/local-ai-command',
    {
      method: 'POST',
      body: JSON.stringify({ instruction: instruction.trim() }),
    },
    sessionToken
  )
  return result.message || '本地 AI 命令已排入桌面端背景工作。'
}
