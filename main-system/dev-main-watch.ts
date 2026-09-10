/**
 * Main process auto-refresh watcher.
 *
 * Watches src-ui/main/** for changes, rebuilds via vite.main.config.ts,
 * and relaunches the Electron app in place (app.relaunch) so the same
 * process tree restarts with the new main bundle. The renderer dev
 * server (port 5173) keeps running and is reconnected on relaunch.
 *
 * Usage: npx ts-node --compiler-options "{\"module\":\"CommonJS\"}" dev-main-watch.ts
 */
import { spawn, spawnSync } from 'node:child_process'
import { existsSync, watchFile } from 'node:fs'
import { resolve } from 'node:path'

const root = __dirname

const MAIN_ENTRY = resolve(root, 'dist-ui/main/index.js')
const RENDERER_DEV_URL = 'http://localhost:5173'

let electronProc: ReturnType<typeof spawn> | null = null
let rebuilding = false
let pendingRelaunch = false

function log(msg: string): void {
  console.log(`[dev-main] ${msg}`)
}

function buildMain(): Promise<void> {
  return new Promise((resolveBuild) => {
    if (rebuilding) {
      pendingRelaunch = true
      resolveBuild()
      return
    }
    rebuilding = true
    log('Rebuilding main process...')
    const result = spawnSync(
      process.platform === 'win32' ? 'npx.cmd' : 'npx',
      ['vite', 'build', '-c', 'vite.main.config.ts'],
      { cwd: root, stdio: 'inherit' },
    )
    rebuilding = false
    if (result.status !== 0) {
      log('Main process build FAILED — keeping current Electron instance')
    } else {
      log('Main process built.')
    }
    resolveBuild()
  })
}

function launchElectron(): void {
  const env = {
    ...process.env,
    GPTBRIDGE_RENDERER_DEV_URL: RENDERER_DEV_URL,
    GPTBRIDGE_MANAGE_BACKEND: '1',
  }
  const exe = resolve(root, 'node_modules/electron/dist/electron.exe')
  log('Launching Electron...')
  electronProc = spawn(exe, ['.'], { cwd: root, stdio: 'inherit', env })
  electronProc.on('close', (code: number | null) => {
    log(`Electron exited with code ${code}`)
    electronProc = null
  })
}

async function relaunchElectron(): Promise<void> {
  // Send a graceful relaunch signal: the main process listens for
  // SIGUSR2 (or a custom env flag) and calls app.relaunch() + app.exit().
  // Fallback: kill + respawn if the signal path is unavailable.
  if (electronProc && !electronProc.killed) {
    log('Relaunching Electron in place...')
    try {
      process.kill(electronProc.pid!, 'SIGUSR2')
      // Wait for the old process to exit; the relaunch spawns a new one.
      await new Promise<void>((r) => {
        const timer = setTimeout(r, 3000)
        electronProc!.once('close', () => {
          clearTimeout(timer)
          r()
        })
      })
    } catch {
      // Signal unsupported — fall back to kill + respawn.
      log('SIGUSR2 unsupported, falling back to kill + respawn...')
      try {
        process.kill(electronProc.pid!)
      } catch {
        // already gone
      }
      electronProc = null
      await new Promise<void>((r) => setTimeout(r, 400))
    }
  }
  if (!electronProc) {
    launchElectron()
  }
}

async function onMainChanged(): Promise<void> {
  log('Main process source changed.')
  await buildMain()
  if (rebuilding) {
    pendingRelaunch = true
    return
  }
  if (pendingRelaunch) {
    pendingRelaunch = false
  }
  await relaunchElectron()
}

async function main(): Promise<void> {
  if (!existsSync(MAIN_ENTRY)) {
    log('Initial main build required...')
    await buildMain()
  }

  // Watch main source directory for changes (debounced via watchFile on entry)
  const mainSource = resolve(root, 'src-ui/main/index.ts')
  if (existsSync(mainSource)) {
    watchFile(mainSource, { interval: 500 }, () => {
      void onMainChanged()
    })
    log(`Watching ${mainSource}`)
  }

  // Also watch the built output as a fallback for multi-file changes
  const preloadSource = resolve(root, 'src-ui/main/preload.ts')
  if (existsSync(preloadSource)) {
    watchFile(preloadSource, { interval: 500 }, () => {
      void onMainChanged()
    })
    log(`Watching ${preloadSource}`)
  }

  launchElectron()
}

main().catch((err) => {
  console.error('[dev-main] Fatal:', err)
  process.exit(1)
})

process.on('SIGINT', () => {
  if (electronProc && !electronProc.killed) {
    try {
      process.kill(electronProc.pid!)
    } catch {
      // already gone
    }
  }
  process.exit(0)
})
process.on('SIGTERM', () => {
  if (electronProc && !electronProc.killed) {
    try {
      process.kill(electronProc.pid!)
    } catch {
      // already gone
    }
  }
  process.exit(0)
})
