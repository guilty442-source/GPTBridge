import crypto from 'node:crypto'
import { spawnSync } from 'node:child_process'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { getRuntimePathLibrary } from './pathLibrary'

const TOKEN_PATTERN = /^[a-f0-9]{64}$/
const TOKEN_FILE_NAME = 'session-token'
const TOKEN_LOCK_NAME = '.session-token.lock'
const LOCK_WAIT_MS = 10_000
const STALE_LOCK_MS = 5_000

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

export function getBackendSessionDescriptor(): {
  token: string
  websocketUrl: string
  workspaceInstanceId: string
} {
  const token = getBackendSessionToken()
  const workspaceInstanceId = getWorkspaceInstanceId()
  return {
    token,
    workspaceInstanceId,
    websocketUrl:
      `ws://127.0.0.1:8765/?token=${encodeURIComponent(token)}` +
      `&instance=${encodeURIComponent(workspaceInstanceId)}`,
  }
}
