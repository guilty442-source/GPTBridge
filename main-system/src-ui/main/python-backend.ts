import { ChildProcess, spawn } from 'child_process'
import fs from 'node:fs'
import http from 'node:http'
import { app } from 'electron'
import {
  getBackendSessionToken,
  getIpcStateRoot,
  getWorkspaceInstanceId,
} from './ipcSession'
import { getRuntimePathLibrary } from './pathLibrary'
import { getRuntimeEnvMap } from './runtime-env'
import { PRODUCT_VERSION } from './product-version'
import {
  createMainSystemGovernanceBootstrap,
  preloadDefaultGovernanceAuthority,
} from './governance-bootstrap'

let pythonProcess: ChildProcess | null = null
type BackendStatus = 'idle' | 'starting' | 'listening' | 'running' | 'stopping' | 'error'

let backendStatus: BackendStatus = 'idle'
let backendStartedAt: number | null = null
let backendReadyAt: number | null = null
let backendMessage = 'backend idle'
let healthTimer: NodeJS.Timeout | null = null
let startupRecoveryTimer: NodeJS.Timeout | null = null
let autonomousRecoveryTimer: NodeJS.Timeout | null = null
let startupRecoveryAttempt = 0
let autonomousRecoveryAttempt = 0
let startSequence = 0
const MAX_STARTUP_RECOVERY_ATTEMPTS = 3
const MAX_AUTONOMOUS_RECOVERY_ATTEMPTS = 3
let backendLastError = ''
type HealthProbeState = 'ready' | 'starting' | 'foreign' | 'untrusted' | 'unreachable'

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
    startupRecoveryActive: startupRecoveryTimer !== null,
    startupRecoveryAttempt,
    autonomousRecoveryActive: autonomousRecoveryTimer !== null,
    autonomousRecoveryAttempt,
  }
}

function clearHealthTimer() {
  if (!healthTimer) return
  clearInterval(healthTimer)
  healthTimer = null
}

function clearStartupRecoveryTimer() {
  if (!startupRecoveryTimer) return
  clearTimeout(startupRecoveryTimer)
  startupRecoveryTimer = null
}

function clearAutonomousRecoveryTimer() {
  if (!autonomousRecoveryTimer) return
  clearTimeout(autonomousRecoveryTimer)
  autonomousRecoveryTimer = null
}

function scheduleAutonomousRecovery(reason: string, sequence: number): void {
  if (sequence !== startSequence || autonomousRecoveryTimer) return
  if (autonomousRecoveryAttempt >= MAX_AUTONOMOUS_RECOVERY_ATTEMPTS) {
    backendStatus = 'error'
    backendMessage = `automatic recovery exhausted: ${reason}`
    return
  }

  autonomousRecoveryAttempt += 1
  const delayMs = 5_000 * autonomousRecoveryAttempt
  backendStatus = 'error'
  backendMessage =
    `automatic backend recovery ${autonomousRecoveryAttempt}/` +
    `${MAX_AUTONOMOUS_RECOVERY_ATTEMPTS} scheduled: ${reason}`
  autonomousRecoveryTimer = setTimeout(() => {
    autonomousRecoveryTimer = null
    if (sequence !== startSequence || pythonProcess) return
    startupRecoveryAttempt = 0
    backendStatus = 'idle'
    startBackend()
  }, delayMs)
}

function scheduleBackendStartupRecovery(reason: string, sequence: number): void {
  if (sequence !== startSequence || startupRecoveryTimer) return
  const paths = getRuntimePathLibrary()
  try {
    preloadDefaultGovernanceAuthority(paths.workspaceRoot)
  } catch {
    backendStatus = 'error'
    backendMessage = 'PERMISSION_DENIED'
    return
  }
  if (startupRecoveryAttempt >= MAX_STARTUP_RECOVERY_ATTEMPTS) {
    backendStatus = 'error'
    backendMessage = `startup recovery exhausted: ${reason}`
    scheduleAutonomousRecovery(reason, sequence)
    return
  }

  startupRecoveryAttempt += 1
  backendStatus = 'starting'
  backendMessage =
    `governance-authorized startup recovery ` +
    `${startupRecoveryAttempt}/${MAX_STARTUP_RECOVERY_ATTEMPTS}: ${reason}`
  startupRecoveryTimer = setTimeout(() => {
    startupRecoveryTimer = null
    if (sequence !== startSequence || pythonProcess) return
    backendStatus = 'idle'
    startBackend()
  }, 500 * startupRecoveryAttempt)
}

function probeBackendHealth(timeoutMs = 700): Promise<HealthProbeState> {
  const expectedInstanceId = getWorkspaceInstanceId()
  return new Promise((resolve) => {
    let settled = false
    const finish = (state: HealthProbeState) => {
      if (settled) return
      settled = true
      resolve(state)
    }

    const request = http.get(
      {
        hostname: '127.0.0.1',
        port: 8765,
        path: '/health',
        timeout: timeoutMs,
      },
      (response) => {
        let body = ''
        response.setEncoding('utf-8')
        response.on('data', (chunk: string) => {
          if (body.length <= 65_536) body += chunk
        })
        response.on('end', () => {
          let instanceId = ''
          let governanceReady = false
          try {
            const payload = JSON.parse(body) as {
              workspace_instance_id?: unknown
              governance_ready?: unknown
            }
            instanceId = String(payload.workspace_instance_id || '')
            governanceReady = payload.governance_ready === true
          } catch {}
          if (instanceId !== expectedInstanceId) {
            finish('foreign')
            return
          }
          if (!governanceReady) {
            finish('untrusted')
            return
          }

          const statusCode = response.statusCode ?? 0
          if (statusCode >= 200 && statusCode < 300) {
            finish('ready')
            return
          }
          if (statusCode === 503) {
            finish('starting')
            return
          }
          finish('unreachable')
        })
      }
    )

    request.on('timeout', () => {
      request.destroy()
      finish('unreachable')
    })
    request.on('error', () => finish('unreachable'))
  })
}

function startHealthPolling(sequence: number) {
  clearHealthTimer()

  const poll = async () => {
    if (sequence !== startSequence) return
    if (backendStatus === 'idle' || backendStatus === 'stopping' || backendStatus === 'error') {
      clearHealthTimer()
      return
    }

    const healthState = await probeBackendHealth()
    if (sequence !== startSequence) return

    if (healthState === 'starting' && backendStatus === 'starting') {
      backendStatus = 'listening'
      backendMessage = 'backend socket listening; waiting for safe router'
      return
    }
    if (healthState === 'foreign') {
      backendStatus = 'error'
      backendMessage = 'port 8765 belongs to a different GPTBridge workspace'
      clearHealthTimer()
      return
    }
    if (healthState === 'untrusted') {
      clearHealthTimer()
      if (pythonProcess) {
        backendStatus = 'starting'
        backendMessage = 'governance authority changed; refreshing backend identity'
        void restartBackend()
      } else {
        backendStatus = 'error'
        backendMessage = 'backend governance identity is not verified'
      }
      return
    }
    if (healthState !== 'ready') return

    backendStatus = 'running'
    startupRecoveryAttempt = 0
    autonomousRecoveryAttempt = 0
    clearStartupRecoveryTimer()
    clearAutonomousRecoveryTimer()
    backendReadyAt = Date.now()
    backendMessage = 'backend health ready'
    clearHealthTimer()
  }

  void poll()
  healthTimer = setInterval(() => {
    void poll()
  }, 500)
}

function spawnBackendProcess(
  paths: ReturnType<typeof getRuntimePathLibrary>,
  sequence: number,
  reclaimUntrustedBackend = false
) {
  backendMessage = 'spawning backend process'
  backendLastError = ''
  console.log(`[Python Backend Manager] Spawning Python backend (${paths.mode})...`)

  const backendArgs = ['-u', paths.pythonEntry, '--serve']
  if (reclaimUntrustedBackend) backendArgs.push('--auto-kill-backend-port')
  backendMessage = 'spawning backend process'
  const runtimeEnvironment = getRuntimeEnvMap()
  const governanceEnvironment = {
    GPTBRIDGE_GOVERNANCE_BOOTSTRAP:
      createMainSystemGovernanceBootstrap(paths.workspaceRoot),
  }

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
        ...governanceEnvironment,
      },
      stdio: ['ignore', 'pipe', 'pipe'],
      windowsHide: true,
    }
  )

  pythonProcess.stdout?.on('data', (data) => {
    const text = data.toString().trim()
    console.log(`[Python STDOUT]: ${text}`)
    if (text.includes('IPC Server running') && backendStatus === 'starting') {
      backendStatus = 'listening'
      backendMessage = 'backend socket listening; waiting for safe router'
    }
  })

  pythonProcess.stderr?.on('data', (data) => {
    const text = data.toString().trim()
    backendLastError = `${backendLastError}\n${text}`.trim().slice(-4_000)
    console.error(`[Python STDERR]: ${text}`)
  })

  pythonProcess.on('exit', (code, signal) => {
    console.log(
      `[Python Backend Manager] Python backend exited with code ${code} and signal ${signal}`
    )
    pythonProcess = null
    clearHealthTimer()

    if (signal === 'SIGTERM') {
      backendStatus = 'idle'
      backendMessage = 'backend stopped'
      return
    }

    void probeBackendHealth().then((healthState) => {
      if (sequence !== startSequence) return
      if (healthState === 'ready') {
        backendStatus = 'running'
        startupRecoveryAttempt = 0
        autonomousRecoveryAttempt = 0
        clearStartupRecoveryTimer()
        clearAutonomousRecoveryTimer()
        backendReadyAt = Date.now()
        backendMessage = 'using existing healthy backend on port 8765'
        return
      }
      if (healthState === 'starting') {
        backendStatus = 'listening'
        backendMessage = 'existing backend is starting on port 8765'
        startHealthPolling(sequence)
        return
      }
      if (healthState === 'foreign') {
        backendStatus = 'error'
        backendMessage = 'port 8765 belongs to a different GPTBridge workspace'
        return
      }
      if (healthState === 'untrusted') {
        backendStatus = 'starting'
        backendMessage = 'replacing backend with a current governance identity'
        spawnBackendProcess(paths, sequence, true)
        return
      }

      scheduleBackendStartupRecovery(
        code === 0
          ? 'backend exited before health ready'
          : `backend exited with code ${code}`,
        sequence
      )
    })
  })

  pythonProcess.on('error', (err) => {
    console.error('[Python Backend Manager] Failed to spawn Python backend:', err)
    pythonProcess = null
    clearHealthTimer()
    scheduleBackendStartupRecovery(err.message, sequence)
  })

  startHealthPolling(sequence)
}

export function startBackend() {
  if (backendStatus === 'running') {
    console.warn('[Python Backend Manager] Python backend already healthy.')
    return
  }

  if (backendStatus === 'starting' || backendStatus === 'listening') {
    console.warn('[Python Backend Manager] Python backend is already starting.')
    return
  }

  if (pythonProcess) {
    console.warn('[Python Backend Manager] Python backend already running.')
    return
  }

  const paths = getRuntimePathLibrary()

  if (!fs.existsSync(paths.pythonExecutable)) {
    const reason = `Python executable not found: ${paths.pythonExecutable}`
    backendMessage = reason
    console.error(
      `[Python Backend Manager] Python executable not found: ${paths.pythonExecutable}`
    )
    console.error(
      `[Python Backend Manager] Candidates: ${paths.pythonExecutableCandidates.join(', ')}`
    )
    scheduleBackendStartupRecovery(reason, startSequence)
    return
  }

  if (!fs.existsSync(paths.pythonEntry)) {
    const reason = `Python entry script not found: ${paths.pythonEntry}`
    backendMessage = reason
    console.error(
      `[Python Backend Manager] Python entry script not found: ${paths.pythonEntry}`
    )
    console.error(
      `[Python Backend Manager] Candidates: ${paths.pythonEntryCandidates.join(', ')}`
    )
    scheduleBackendStartupRecovery(reason, startSequence)
    return
  }

  backendStatus = 'starting'
  backendStartedAt = Date.now()
  backendReadyAt = null
  backendMessage = 'checking existing backend on port 8765'
  startSequence += 1
  const sequence = startSequence

  void probeBackendHealth(300).then((healthState) => {
    if (sequence !== startSequence || backendStatus === 'stopping') return
    if (healthState === 'ready') {
    backendStatus = 'running'
    startupRecoveryAttempt = 0
    autonomousRecoveryAttempt = 0
    clearStartupRecoveryTimer()
    clearAutonomousRecoveryTimer()
      backendReadyAt = Date.now()
      backendMessage = 'using existing healthy backend on port 8765'
      return
    }
    if (healthState === 'starting') {
      backendStatus = 'listening'
      backendMessage = 'existing backend is starting on port 8765'
      startHealthPolling(sequence)
      return
    }
    if (healthState === 'foreign') {
      backendStatus = 'error'
      backendMessage = 'port 8765 belongs to a different GPTBridge workspace'
      return
    }
    if (healthState === 'untrusted') {
      backendStatus = 'starting'
      backendMessage = 'replacing backend with a current governance identity'
      spawnBackendProcess(paths, sequence, true)
      return
    }

    spawnBackendProcess(paths, sequence)
  })
}

export function ensureBackendStarted(): BackendStatus {
  if (backendStatus === 'error') {
    startupRecoveryAttempt = 0
    clearAutonomousRecoveryTimer()
    backendStatus = 'idle'
  }
  if (!pythonProcess && backendStatus !== 'running' && backendStatus !== 'starting' && backendStatus !== 'listening') {
    startBackend()
  }
  return backendStatus
}

export async function stopBackend() {
  clearStartupRecoveryTimer()
  clearAutonomousRecoveryTimer()
  if (!pythonProcess) {
    if (backendStatus === 'starting' || backendStatus === 'listening') {
      backendStatus = 'idle'
      backendMessage = 'backend start cancelled'
      startSequence += 1
      clearHealthTimer()
      return
    }
    console.warn('[Python Backend Manager] Python backend not running.')
    return
  }

  backendStatus = 'stopping'
  backendMessage = 'stopping backend'
  startSequence += 1
  clearHealthTimer()
  console.log('[Python Backend Manager] Stopping Python backend...')
  const processToStop = pythonProcess
  const processId = processToStop.pid
  const paths = getRuntimePathLibrary()
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
    const timer = setTimeout(() => finish(false), 1_500)
  })

  if (!exitedGracefully) {
    if (process.platform === 'win32' && processId) {
      await new Promise<void>((resolve) => {
        const terminator = spawn(
          'taskkill.exe',
          ['/PID', String(processId), '/T', '/F'],
          { windowsHide: true, stdio: 'ignore' }
        )
        terminator.once('exit', () => resolve())
        terminator.once('error', () => resolve())
      })
    } else {
      processToStop.kill('SIGTERM')
    }
  }

  if (pythonProcess === processToStop) pythonProcess = null
  backendStatus = 'idle'
  backendMessage = exitedGracefully
    ? 'backend stopped gracefully'
    : 'backend process tree stopped after graceful timeout'
}

export async function restartBackend() {
  if (pythonProcess) {
    await stopBackend()
  }
  startBackend()
  return backendStatus
}
