import { ChildProcess, spawn } from 'child_process'
import fs from 'node:fs'
import http from 'node:http'
import { getRuntimePathLibrary } from './pathLibrary'
import { getRuntimeEnvMap } from './runtime-env'

let pythonProcess: ChildProcess | null = null
type BackendStatus = 'idle' | 'starting' | 'listening' | 'running' | 'stopping' | 'error'

let backendStatus: BackendStatus = 'idle'
let backendStartedAt: number | null = null
let backendReadyAt: number | null = null
let backendMessage = 'backend idle'
let healthTimer: NodeJS.Timeout | null = null
let startSequence = 0
type HealthProbeState = 'ready' | 'starting' | 'unreachable'

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
  }
}

function clearHealthTimer() {
  if (!healthTimer) return
  clearInterval(healthTimer)
  healthTimer = null
}

function probeBackendHealth(timeoutMs = 700): Promise<HealthProbeState> {
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
        response.resume()
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
    if (healthState !== 'ready') return

    backendStatus = 'running'
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
  sequence: number
) {
  backendMessage = 'spawning backend process'
  console.log(`[Python Backend Manager] Spawning Python backend (${paths.mode})...`)

  pythonProcess = spawn(
    paths.pythonExecutable,
    ['-u', paths.pythonEntry, '--serve'],
    {
      cwd: paths.workspaceRoot,
      env: {
        ...getRuntimeEnvMap(),
        GPTBRIDGE_PROJECT_ROOT: paths.workspaceRoot,
      },
      stdio: ['ignore', 'pipe', 'pipe'],
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
    console.error(`[Python STDERR]: ${data.toString().trim()}`)
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

      backendStatus = code === 0 ? 'idle' : 'error'
      backendMessage =
        code === 0 ? 'backend exited before health ready' : `backend exited with code ${code}`
    })
  })

  pythonProcess.on('error', (err) => {
    console.error('[Python Backend Manager] Failed to spawn Python backend:', err)
    pythonProcess = null
    backendStatus = 'error'
    backendMessage = err.message
    clearHealthTimer()
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
    backendStatus = 'error'
    backendMessage = `Python executable not found: ${paths.pythonExecutable}`
    console.error(
      `[Python Backend Manager] Python executable not found: ${paths.pythonExecutable}`
    )
    console.error(
      `[Python Backend Manager] Candidates: ${paths.pythonExecutableCandidates.join(', ')}`
    )
    return
  }

  if (!fs.existsSync(paths.pythonEntry)) {
    backendStatus = 'error'
    backendMessage = `Python entry script not found: ${paths.pythonEntry}`
    console.error(
      `[Python Backend Manager] Python entry script not found: ${paths.pythonEntry}`
    )
    console.error(
      `[Python Backend Manager] Candidates: ${paths.pythonEntryCandidates.join(', ')}`
    )
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

    spawnBackendProcess(paths, sequence)
  })
}

export function ensureBackendStarted() {
  if (!pythonProcess && backendStatus !== 'running' && backendStatus !== 'starting' && backendStatus !== 'listening') {
    startBackend()
  }
  return backendStatus
}

export async function stopBackend() {
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
  pythonProcess.kill('SIGTERM')

  await new Promise<void>((resolve) => {
    if (!pythonProcess) {
      resolve()
      return
    }

    pythonProcess.once('exit', () => {
      pythonProcess = null
      backendStatus = 'idle'
      resolve()
    })
  })
}

export async function restartBackend() {
  if (pythonProcess) {
    await stopBackend()
  }
  startBackend()
  return backendStatus
}
