/**
 * python-backend — Electron-side boot_core process manager.
 *
 * Architecture boundary (A60/A61):
 *   啟動入口 (Electron main) 僅啟動頁面；後續交由啟動核心 (boot_core)
 *   去啟動主宰 (decision sovereign)。
 *
 * A60: LAUNCHER:interface-presentation-only; BOOT-OPERATIONS:none.
 * The launcher must NOT generate governance bootstrap material
 * (FORBID:governance-system-start).  boot_core generates its own fresh
 * governance bootstrap token per spawn in ``_generate_governance_bootstrap``.
 *
 * This module ONLY spawns and stops the boot_core process.  It does NOT:
 *   - generate governance bootstrap tokens (boot_core does this)
 *   - probe backend HTTP health (boot_core supervises this)
 *   - perform auto-recovery or source repair (boot_core handles restarts)
 *
 * The Electron UI queries backend status via IPC commands to the backend
 * itself; this module only tracks whether boot_core is alive.
 */
import { ChildProcess, spawn } from 'child_process'
import crypto from 'node:crypto'
import fs from 'node:fs'
import http from 'node:http'
import path from 'node:path'
import { app } from 'electron'
import {
  getBackendSessionToken,
  getIpcStateRoot,
  getWorkspaceInstanceId,
} from './ipcSession'
import { getRuntimePathLibrary } from './pathLibrary'
import { getRuntimeEnvMap } from './runtime-env'
import { PRODUCT_VERSION } from './product-version'

let pythonProcess: ChildProcess | null = null
type BackendStatus = 'idle' | 'starting' | 'running' | 'stopping' | 'error'

let backendStatus: BackendStatus = 'idle'
let backendStartedAt: number | null = null
let backendReadyAt: number | null = null
let backendMessage = 'backend idle'
let backendLastError = ''

// Auto-restart configuration
const AUTO_RESTART_MAX_ATTEMPTS = 5
const AUTO_RESTART_BASE_DELAY_MS = 2000
const AUTO_RESTART_MAX_DELAY_MS = 30000
let autoRestartAttempts = 0
let autoRestartTimer: ReturnType<typeof setTimeout> | null = null
let manualShutdown = false
let shutdownToken = ''

function probeExistingBackend(): Promise<boolean> {
  return new Promise((resolve) => {
    const request = http.get(
      { host: '127.0.0.1', port: 8765, path: '/health?brief=1', timeout: 8_000 },
      (response) => {
        const chunks: Buffer[] = []
        response.on('data', (chunk) => chunks.push(Buffer.from(chunk)))
        response.on('end', () => {
          try {
            const payload = JSON.parse(Buffer.concat(chunks).toString('utf8')) as {
              workspace_instance_id?: string
              version?: string
              backend_runtime_ready?: boolean
            }
            resolve(
              payload.workspace_instance_id === getWorkspaceInstanceId() &&
                payload.version === PRODUCT_VERSION &&
                payload.backend_runtime_ready === true
            )
          } catch {
            resolve(false)
          }
        })
      }
    )
    request.on('timeout', () => request.destroy())
    request.on('error', () => resolve(false))
  })
}

function hasLiveSupervisor(paths: ReturnType<typeof getRuntimePathLibrary>): boolean {
  const statePath = path.join(
    paths.workspaceRoot,
    'main-system',
    'runtime',
    'state',
    'boot-core.json'
  )
  try {
    const state = JSON.parse(fs.readFileSync(statePath, 'utf8')) as {
      pid?: number
      status?: string
    }
    const pid = Number(state.pid)
    if (pid <= 0 || state.status === 'stopped') return false
    process.kill(pid, 0)
    return true
  } catch {
    return false
  }
}

function requestGracefulBackendShutdown(): Promise<boolean> {
  if (!shutdownToken) return Promise.resolve(false)
  return new Promise((resolve) => {
    const request = http.request(
      {
        method: 'GET',
        host: '127.0.0.1',
        port: 8765,
        path: '/shutdown',
        timeout: 1_500,
        headers: {
          'X-GPTBridge-Shutdown-Token': shutdownToken,
          'X-GPTBridge-Shutdown-Reason': 'hot-update',
        },
      },
      (response) => {
        response.resume()
        response.on('end', () => resolve(response.statusCode === 200))
      }
    )
    request.on('timeout', () => request.destroy())
    request.on('error', () => resolve(false))
    request.end()
  })
}

export function getBackendStatus(): BackendStatus {
  return backendStatus
}

export function getBackendRuntimeInfo() {
  return {
    status: backendStatus,
    ready: backendStatus === 'running',
    startedAt: backendStartedAt,
    readyAt: backendReadyAt,
    startupMs:
      backendStartedAt && backendReadyAt ? backendReadyAt - backendStartedAt : null,
    message: backendMessage,
    lastError: backendLastError,
  }
}

function spawnBootCore(
  paths: ReturnType<typeof getRuntimePathLibrary>,
  autoKillBackendPort = false
): void {
  // Shutdown-in-progress guard: a startBackend() call still awaiting its
  // probe/fs checks when the UI closed must not spawn an orphan backend.
  if (manualShutdown) {
    backendStatus = 'idle'
    backendMessage = 'backend start cancelled by shutdown'
    return
  }
  backendMessage = 'spawning boot_core (startup core)'
  backendLastError = ''
  console.log('[Python Backend Manager] Spawning boot_core...')

  try {
    // Entry responsibility boundary: the launcher wakes the screen and spawns
    // the startup core (boot_core); boot_core awakens and supervises the system
    // backend (main.py).  Governance bootstrap token is generated by boot_core,
    // NOT by Electron.
    const backendArgs = ['-u', paths.bootCoreEntry, '--serve']
    if (autoKillBackendPort) backendArgs.push('--auto-kill-backend-port')

    const runtimeEnvironment = getRuntimeEnvMap()
    shutdownToken = crypto.randomBytes(32).toString('hex')
    // A60: the launcher must NOT generate governance bootstrap material
    // (FORBID:governance-system-start).  boot_core generates its own fresh
    // governance bootstrap token per spawn in ``_generate_governance_bootstrap``
    // when GPTBRIDGE_GOVERNANCE_BOOTSTRAP is not set.
    pythonProcess = spawn(
      paths.pythonExecutable,
      backendArgs,
      {
        cwd: paths.workspaceRoot,
        env: {
          ...runtimeEnvironment,
          GPTBRIDGE_PROJECT_ROOT: paths.workspaceRoot,
          GPTBRIDGE_APP_VERSION: PRODUCT_VERSION,
          GPTBRIDGE_IPC_STATE_ROOT: getIpcStateRoot(),
          GPTBRIDGE_IPC_SESSION_TOKEN: getBackendSessionToken(),
          GPTBRIDGE_SHUTDOWN_TOKEN: shutdownToken,
        },
        detached: true,
        stdio: 'ignore',
        windowsHide: true,
      }
    )

    pythonProcess.on('exit', (code, signal) => {
      console.log(
        `[Python Backend Manager] boot_core exited with code ${code} and signal ${signal}`
      )
      pythonProcess = null

      if (signal === 'SIGTERM' || code === 0 || manualShutdown) {
        backendStatus = 'idle'
        backendMessage = 'backend stopped'
        autoRestartAttempts = 0
        manualShutdown = false
        return
      }

      // Unexpected exit — attempt auto-restart
      backendStatus = 'error'
      backendMessage = `boot_core exited unexpectedly (code ${code}), auto-restarting...`
      scheduleAutoRestart()
    })

    pythonProcess.on('error', (err) => {
      console.error('[Python Backend Manager] Failed to spawn boot_core:', err)
      pythonProcess = null
      backendStatus = 'error'
      backendMessage = `boot_core spawn failed: ${err.message}`
      scheduleAutoRestart()
    })

    // boot_core is now responsible for starting and supervising main.py.
    // Electron considers the backend "running" once boot_core is alive;
    // detailed health status is available via IPC to the backend itself.
    backendStatus = 'running'
    backendReadyAt = Date.now()
    backendMessage = 'boot_core supervising backend'
    autoRestartAttempts = 0
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err)
    console.error('[Python Backend Manager] Failed to spawn boot_core:', err)
    pythonProcess = null
    backendStatus = 'error'
    backendMessage = `boot_core spawn failed: ${message}`
    scheduleAutoRestart()
  }
}

function scheduleAutoRestart(): void {
  if (autoRestartTimer) {
    clearTimeout(autoRestartTimer)
    autoRestartTimer = null
  }
  if (autoRestartAttempts >= AUTO_RESTART_MAX_ATTEMPTS) {
    backendMessage = `boot_core auto-restart exhausted (${AUTO_RESTART_MAX_ATTEMPTS} attempts), giving up`
    console.error(`[Python Backend Manager] ${backendMessage}`)
    return
  }
  autoRestartAttempts++
  const delay = Math.min(
    AUTO_RESTART_MAX_DELAY_MS,
    AUTO_RESTART_BASE_DELAY_MS * Math.pow(2, autoRestartAttempts - 1)
  )
  console.log(
    `[Python Backend Manager] Auto-restart attempt ${autoRestartAttempts}/${AUTO_RESTART_MAX_ATTEMPTS} in ${delay}ms`
  )
  autoRestartTimer = setTimeout(() => {
    autoRestartTimer = null
    if (manualShutdown) return
    console.log('[Python Backend Manager] Auto-restarting boot_core...')
    void startBackend()
  }, delay)
}

export async function startBackend(forceReplacement = false) {
  if (backendStatus === 'running') {
    console.warn('[Python Backend Manager] boot_core already running.')
    return
  }

  if (pythonProcess) {
    console.warn('[Python Backend Manager] boot_core process already exists.')
    return
  }

  const paths = getRuntimePathLibrary()

  if (
    !forceReplacement &&
    (await probeExistingBackend()) &&
    hasLiveSupervisor(paths)
  ) {
    backendStatus = 'running'
    backendReadyAt = Date.now()
    backendMessage = 'attached to existing governed backend'
    return
  }

  if (!fs.existsSync(paths.pythonExecutable)) {
    backendStatus = 'error'
    backendMessage = `Python executable not found: ${paths.pythonExecutable}`
    console.error(
      `[Python Backend Manager] Python executable not found: ${paths.pythonExecutable}`
    )
    return
  }

  if (!fs.existsSync(paths.bootCoreEntry)) {
    backendStatus = 'error'
    backendMessage = `boot_core entry not found: ${paths.bootCoreEntry}`
    console.error(
      `[Python Backend Manager] boot_core entry not found: ${paths.bootCoreEntry}`
    )
    return
  }

  if (!fs.existsSync(paths.pythonEntry)) {
    backendStatus = 'error'
    backendMessage = `Python entry not found: ${paths.pythonEntry}`
    console.error(
      `[Python Backend Manager] Python entry not found: ${paths.pythonEntry}`
    )
    return
  }

  if (manualShutdown) {
    backendStatus = 'idle'
    backendMessage = 'backend start cancelled by shutdown'
    return
  }

  backendStatus = 'starting'
  backendStartedAt = Date.now()
  backendReadyAt = null
  backendMessage = 'spawning boot_core'
  spawnBootCore(paths, true)
}

export async function ensureBackendStarted(): Promise<BackendStatus> {
  if (backendStatus === 'error') {
    backendStatus = 'idle'
  }
  if (!pythonProcess && backendStatus !== 'running' && backendStatus !== 'starting') {
    await startBackend()
  }
  return backendStatus
}

export async function stopBackend() {
  manualShutdown = true
  if (autoRestartTimer) {
    clearTimeout(autoRestartTimer)
    autoRestartTimer = null
  }
  if (!pythonProcess) {
    if (backendStatus === 'starting') {
      backendStatus = 'idle'
      backendMessage = 'backend start cancelled'
    }
    return
  }

  backendStatus = 'stopping'
  backendMessage = 'stopping boot_core'
  console.log('[Python Backend Manager] Stopping boot_core...')
  const processToStop = pythonProcess
  const processId = processToStop.pid
  await requestGracefulBackendShutdown()
  // Graceful shutdown window: a full backend shutdown stops every tool
  // backend, the sovereign stack, services, and flushes state — that takes
  // longer than a bare socket close.  Give it a bounded window well above
  // the internal task deadline before falling back to force-kill.
  const GRACEFUL_EXIT_MS = 10_000
  const exitedGracefully = await new Promise<boolean>((resolve) => {
    if (processToStop.exitCode !== null || processToStop.signalCode !== null) {
      resolve(true)
      return
    }
    let settled = false
    const finish = (value: boolean) => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      resolve(value)
    }
    processToStop.once('exit', () => finish(true))
    const timer = setTimeout(() => finish(false), GRACEFUL_EXIT_MS)
  })

  const taskkillPid = (pid: number): Promise<void> =>
    new Promise<void>((resolve) => {
      const terminator = spawn(
        'taskkill.exe',
        ['/PID', String(pid), '/F'],
        { windowsHide: true, stdio: 'ignore' }
      )
      const done = () => resolve()
      terminator.once('exit', done)
      terminator.once('error', done)
      // taskkill that never reports must not block the quit path.
      setTimeout(done, 5_000)
    })

  const killBackendPid = async (): Promise<void> => {
    // Stop the main backend process (main.py) by PID without /T so that
    // independent tool processes (detached in their own process group) are
    // not cascade-killed when the main-system shuts down.
    try {
      const paths = getRuntimePathLibrary()
      const statePath = path.join(
        paths.workspaceRoot,
        'main-system',
        'runtime',
        'state',
        'boot-core.json'
      )
      if (fs.existsSync(statePath)) {
        const state = JSON.parse(fs.readFileSync(statePath, 'utf8')) as {
          backend_pid?: number
        }
        const backendPid = Number(state.backend_pid)
        if (backendPid > 0 && backendPid !== processId) {
          // Skip if the backend already exited on its own.
          try {
            process.kill(backendPid, 0)
          } catch {
            return
          }
          if (process.platform === 'win32') {
            await taskkillPid(backendPid)
          } else {
            try {
              process.kill(backendPid, 'SIGTERM')
            } catch {
              // already gone
            }
          }
        }
      }
    } catch {
      // Best-effort main backend termination; its absence must not block the
      // UI from quitting or independent tools from continuing.
    }
  }

  if (!exitedGracefully && process.platform === 'win32' && processId) {
    await taskkillPid(processId)
  } else if (!exitedGracefully) {
    processToStop.kill('SIGTERM')
  }
  // Complete-close guarantee: even when boot_core exited first, a still-
  // running backend_pid would survive as an orphan — always verify and
  // terminate it, not only on the force-kill path.
  if (!exitedGracefully) {
    await killBackendPid()
  }

  if (pythonProcess === processToStop) pythonProcess = null
  shutdownToken = ''
  backendStatus = 'idle'
  backendMessage = exitedGracefully
    ? 'backend stopped gracefully'
    : 'boot_core process tree stopped after graceful timeout'
}

export async function restartBackend(): Promise<BackendStatus> {
  manualShutdown = false
  if (autoRestartTimer) {
    clearTimeout(autoRestartTimer)
    autoRestartTimer = null
  }
  if (pythonProcess) {
    await stopBackend()
  }
  backendStatus = 'idle'
  manualShutdown = false
  await startBackend(true)
  return getBackendStatus()
}
