import crypto from 'node:crypto'
import { spawnSync } from 'node:child_process'
import fs from 'node:fs'
import http from 'node:http'
import os from 'node:os'
import path from 'node:path'
import { getRuntimePathLibrary } from './pathLibrary'

const TOKEN_PATTERN = /^[a-f0-9]{64}$/
const TOKEN_FILE_NAME = 'session-token'
const TOKEN_LOCK_NAME = '.session-token.lock'
const LOCK_WAIT_MS = 10_000
const STALE_LOCK_MS = 5_000
const DEFAULT_GATEWAY_PORT = 8765
const GENERATION_PORT_OFFSETS = [1, 2] as const
const HEALTH_PROBE_TIMEOUT_MS = 350
const STARTUP_MANIFEST_RELATIVE = [
  'main-system',
  'config',
  'startup_manifest.json',
] as const
const BOOT_CORE_STATE_RELATIVE = [
  'main-system',
  'runtime',
  'state',
  'boot-core.json',
] as const
export const LOOPBACK_HOST = '127.0.0.1'
export const BACKEND_HEALTH_PATH = '/health?brief=1'

let cachedToken = ''

function sleepSync(milliseconds: number): void {
  Atomics.wait(
    new Int32Array(new SharedArrayBuffer(Int32Array.BYTES_PER_ELEMENT)),
    0,
    0,
    milliseconds
  )
}

function hardenPrivatePath(
  targetPath: string,
  { directory = false }: { directory?: boolean } = {}
): void {
  try {
    fs.chmodSync(targetPath, directory ? 0o700 : 0o600)
  } catch {}
  if (process.platform !== 'win32') return
  const username = String(process.env.USERNAME || '').trim()
  if (!username) return
  const userPermission = directory ? '(OI)(CI)(F)' : '(R,W)'
  const systemPermission = directory ? '(OI)(CI)(F)' : '(F)'
  try {
    spawnSync(
      'icacls.exe',
      [
        targetPath,
        '/inheritance:r',
        '/grant:r',
        `${username}:${userPermission}`,
        '/grant:r',
        `*S-1-5-18:${systemPermission}`,
      ],
      { windowsHide: true, stdio: 'ignore' }
    )
  } catch {}
}

export function getIpcStateRoot(): string {
  const configured = String(process.env.GPTBRIDGE_IPC_STATE_ROOT || '').trim()
  if (configured) return path.resolve(configured)

  if (process.platform === 'win32') {
    const localAppData = String(process.env.LOCALAPPDATA || '').trim()
    const base = localAppData || path.join(os.homedir(), 'AppData', 'Local')
    return path.resolve(base, 'GPTBridge', 'ipc')
  }

  const xdgStateHome = String(process.env.XDG_STATE_HOME || '').trim()
  const base = xdgStateHome || path.join(os.homedir(), '.local', 'state')
  return path.resolve(base, 'GPTBridge', 'ipc')
}

function tokenFilePath(): string {
  return path.join(getIpcStateRoot(), TOKEN_FILE_NAME)
}

function readToken(filePath: string): string {
  try {
    const token = fs.readFileSync(filePath, 'utf-8').trim().toLowerCase()
    return TOKEN_PATTERN.test(token) ? token : ''
  } catch {
    return ''
  }
}

function breakStaleLock(lockPath: string): void {
  let ageMs = 0
  try {
    ageMs = Date.now() - fs.lstatSync(lockPath).mtimeMs
  } catch {
    return
  }
  if (ageMs < STALE_LOCK_MS) return

  const stalePath = `${lockPath}.stale-${process.pid}-${crypto.randomBytes(6).toString('hex')}`
  try {
    fs.renameSync(lockPath, stalePath)
  } catch {
    return
  }
  try {
    fs.rmdirSync(stalePath)
  } catch {
    // A non-empty stale lock is left quarantined. The canonical lock name is
    // free, so recovery can continue without deleting unknown contents.
  }
}

function writeTokenAtomically(filePath: string, token: string): void {
  const temporaryPath = path.join(
    path.dirname(filePath),
    `.${TOKEN_FILE_NAME}.${process.pid}.${crypto.randomBytes(8).toString('hex')}.tmp`
  )
  let descriptor: number | null = null
  try {
    descriptor = fs.openSync(temporaryPath, 'wx', 0o600)
    fs.writeFileSync(descriptor, `${token}\n`, 'utf-8')
    fs.fsyncSync(descriptor)
    fs.closeSync(descriptor)
    descriptor = null
    fs.renameSync(temporaryPath, filePath)
  } finally {
    if (descriptor !== null) {
      try {
        fs.closeSync(descriptor)
      } catch {}
    }
    try {
      fs.unlinkSync(temporaryPath)
    } catch {}
  }
}

function repairOrCreateToken(filePath: string): string {
  const stateRoot = path.dirname(filePath)
  fs.mkdirSync(stateRoot, { recursive: true, mode: 0o700 })
  hardenPrivatePath(stateRoot, { directory: true })

  const lockPath = path.join(stateRoot, TOKEN_LOCK_NAME)
  const deadline = Date.now() + LOCK_WAIT_MS
  let ownsLock = false
  let ownerNonce = ''

  while (!ownsLock) {
    const racedToken = readToken(filePath)
    if (racedToken) return racedToken
    let createdLock = false
    try {
      fs.mkdirSync(lockPath, { mode: 0o700 })
      createdLock = true
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== 'EEXIST') throw error
    }
    if (createdLock) {
      try {
        ownerNonce = crypto.randomBytes(16).toString('hex')
        fs.writeFileSync(path.join(lockPath, 'owner'), `${ownerNonce}\n`, {
          encoding: 'ascii',
          flag: 'wx',
          mode: 0o600,
        })
        ownsLock = true
        break
      } catch (error) {
        try {
          fs.unlinkSync(path.join(lockPath, 'owner'))
        } catch {}
        try {
          fs.rmdirSync(lockPath)
        } catch {}
        throw error
      }
    }

    breakStaleLock(lockPath)
    if (Date.now() >= deadline) {
      const finalToken = readToken(filePath)
      if (finalToken) return finalToken
      throw new Error(`Timed out acquiring IPC token lock: ${lockPath}`)
    }
    sleepSync(25)
  }

  const stillOwnsLock = (): boolean => {
    if (!ownsLock || !ownerNonce) return false
    try {
      return (
        fs.readFileSync(path.join(lockPath, 'owner'), 'ascii').trim() ===
        ownerNonce
      )
    } catch {
      return false
    }
  }

  try {
    const racedToken = readToken(filePath)
    if (racedToken) return racedToken
    if (!stillOwnsLock()) throw new Error('Lost IPC token repair lock')
    fs.utimesSync(lockPath, new Date(), new Date())

    try {
      fs.lstatSync(filePath)
      const quarantinePath = path.join(
        stateRoot,
        `${TOKEN_FILE_NAME}.invalid-${Date.now()}-${process.pid}-${crypto
          .randomBytes(6)
          .toString('hex')}`
      )
      fs.renameSync(filePath, quarantinePath)
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error
    }

    const generated = crypto.randomBytes(32).toString('hex')
    writeTokenAtomically(filePath, generated)
    hardenPrivatePath(filePath)
    const persisted = readToken(filePath)
    if (!persisted) throw new Error('IPC session token write verification failed')
    return persisted
  } finally {
    if (stillOwnsLock()) {
      try {
        fs.unlinkSync(path.join(lockPath, 'owner'))
        fs.rmdirSync(lockPath)
      } catch {}
    }
  }
}

/**
 * Return the per-user IPC capability token shared by the main application
 * and standalone platform-tool windows.
 *
 * The token isn't a defence against another process running as the same OS
 * account. It prevents untrusted browser/file origins from driving the local
 * WebSocket command surface without first crossing the Electron preload bridge.
 */
export function getBackendSessionToken(): string {
  if (cachedToken) return cachedToken
  const configured = String(
    process.env.GPTBRIDGE_IPC_SESSION_TOKEN || ''
  ).trim().toLowerCase()
  if (TOKEN_PATTERN.test(configured)) {
    cachedToken = configured
    return cachedToken
  }

  const filePath = tokenFilePath()
  const existing = readToken(filePath)
  if (existing) {
    hardenPrivatePath(path.dirname(filePath), { directory: true })
    hardenPrivatePath(filePath)
    cachedToken = existing
    return cachedToken
  }

  cachedToken = repairOrCreateToken(filePath)
  return cachedToken
}

export function getWorkspaceInstanceId(): string {
  let normalized = path
    .resolve(getRuntimePathLibrary().workspaceRoot)
    .replace(/\\/g, '/')
  if (process.platform === 'win32') normalized = normalized.toLowerCase()
  return crypto
    .createHash('sha256')
    .update(normalized, 'utf-8')
    .digest('hex')
    .slice(0, 24)
}

function isValidPort(value: unknown): value is number {
  return (
    typeof value === 'number' &&
    Number.isInteger(value) &&
    value > 0 &&
    value <= 65535
  )
}

function readConfiguredGatewayPort(): number {
  const manifestPath = path.join(
    getRuntimePathLibrary().workspaceRoot,
    ...STARTUP_MANIFEST_RELATIVE
  )
  try {
    const manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf-8')) as {
      ports?: { health_probe?: unknown }
    }
    const port = Number(manifest.ports?.health_probe)
    return isValidPort(port) ? port : DEFAULT_GATEWAY_PORT
  } catch {
    return DEFAULT_GATEWAY_PORT
  }
}

function readActiveBackendPort(): number | null {
  const statePath = path.join(
    getRuntimePathLibrary().workspaceRoot,
    ...BOOT_CORE_STATE_RELATIVE
  )
  try {
    const state = JSON.parse(fs.readFileSync(statePath, 'utf-8')) as {
      active_backend_port?: unknown
    }
    const port = Number(state.active_backend_port)
    return isValidPort(port) ? port : null
  } catch {
    return null
  }
}

function probeBackendPort(port: number): Promise<boolean> {
  return new Promise((resolve) => {
    let settled = false
    const finish = (result: boolean) => {
      if (settled) return
      settled = true
      resolve(result)
    }
    const request = http.get(
      {
        host: LOOPBACK_HOST,
        port,
        path: BACKEND_HEALTH_PATH,
        timeout: HEALTH_PROBE_TIMEOUT_MS,
      },
      (response) => {
        const chunks: Buffer[] = []
        response.on('data', (chunk) => chunks.push(Buffer.from(chunk)))
        response.on('end', () => {
          try {
            const payload = JSON.parse(Buffer.concat(chunks).toString('utf8')) as {
              workspace_instance_id?: unknown
            }
            finish(payload.workspace_instance_id === getWorkspaceInstanceId())
          } catch {
            finish(false)
          }
        })
      }
    )
    request.on('timeout', () => request.destroy())
    request.on('error', () => finish(false))
  })
}

/**
 * Resolve the live backend endpoint without assuming a fixed port.
 *
 * The startup manifest declares the gateway port and boot_core rotates the
 * backend generation across the two following ports.  Prefer the configured
 * gateway, then the generation recorded by boot_core, then the remaining
 * generation ports — returning the first endpoint that answers /health for
 * this workspace instance.  Falls back to the configured gateway so callers
 * still have a stable default while the backend is down.
 */
export async function resolveBackendPort(): Promise<number> {
  const configured = readConfiguredGatewayPort()
  const candidates = [
    configured,
    readActiveBackendPort(),
    ...GENERATION_PORT_OFFSETS.map((offset) => configured + offset),
  ].filter(isValidPort)
  const unique = [...new Set(candidates)]
  // Probe all candidates in parallel — preference order is preserved by
  // picking the first candidate whose probe succeeded, so a dead preferred
  // port no longer serializes the timeout wait behind each live check.
  const results = await Promise.all(unique.map((port) => probeBackendPort(port)))
  const resolved = unique.find((_, index) => results[index])
  return resolved ?? configured
}

/**
 * Probe the configured gateway port for a live boot_core.
 *
 * boot_core hosts the gateway, so a gateway that answers /health for this
 * workspace is authoritative evidence that a supervisor is alive — even when
 * the persisted boot-core state file is missing or mentions a superseded
 * generation.  Used to avoid spawning a duplicate supervisor.
 */
export async function isGatewayAlive(): Promise<boolean> {
  return probeBackendPort(readConfiguredGatewayPort())
}

function createWebSocketSessionTicket(
  token: string,
  workspaceInstanceId: string
): string {
  const expiresAt = Math.floor(Date.now() / 1000) + 30
  const nonce = crypto.randomBytes(16).toString('hex')
  const payload = `${expiresAt}.${nonce}.${workspaceInstanceId}`
  const signature = crypto
    .createHmac('sha256', token)
    .update(payload, 'utf8')
    .digest('hex')
  return `${payload}.${signature}`
}

export async function getBackendSessionDescriptor(): Promise<{
  websocketUrl: string
  workspaceInstanceId: string
}> {
  const token = getBackendSessionToken()
  const workspaceInstanceId = getWorkspaceInstanceId()
  const backendPort = await resolveBackendPort()
  const ticket = createWebSocketSessionTicket(token, workspaceInstanceId)
  return {
    workspaceInstanceId,
    websocketUrl:
      `ws://${LOOPBACK_HOST}:${backendPort}/?ticket=${encodeURIComponent(ticket)}` +
      `&instance=${encodeURIComponent(workspaceInstanceId)}`,
  }
}
