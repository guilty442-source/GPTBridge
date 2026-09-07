/**
 * Dev launcher: starts Vite dev server + Electron with HMR.
 *
 * No build step needed — renderer source is served directly by Vite.
 * Main process is built once (or reused if already built).
 *
 * Usage: npm run dev
 */
import { spawn, spawnSync } from 'node:child_process'
import { existsSync } from 'node:fs'
import { resolve, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const __dirname = dirname(fileURLToPath(import.meta.url))
const root = resolve(__dirname, '..')

const RENDERER_DEV_URL = 'http://localhost:5173'
const MAIN_ENTRY = resolve(root, 'dist-ui/main/index.js')
const MAIN_SOURCE = resolve(root, 'src-ui/main/index.ts')

// ── Step 1: Ensure main process is built ──────────────────────────
if (!existsSync(MAIN_ENTRY)) {
  console.log('[dev] Building main process (one-time)...')
  const result = spawnSync(
    process.platform === 'win32' ? 'npx.cmd' : 'npx',
    ['vite', 'build', '-c', 'vite.main.config.ts'],
    { cwd: root, stdio: 'inherit' }
  )
  if (result.status !== 0) {
    console.error('[dev] Main process build failed')
    process.exit(1)
  }
  console.log('[dev] Main process built.')
}

// ── Step 2: Start Vite dev server ─────────────────────────────────
console.log('[dev] Starting Vite dev server...')
const vite = spawn(
  process.platform === 'win32' ? 'npx.cmd' : 'npx',
  ['vite', '--port', '5173', '--strictPort'],
  { cwd: root, stdio: 'pipe' }
)

let viteReady = false
vite.stdout.on('data', (data) => {
  const text = data.toString()
  process.stdout.write(text)
  if (!viteReady && text.includes('Local:')) {
    viteReady = true
    console.log('[dev] Vite ready, launching Electron...')
    launchElectron()
  }
})
vite.stderr.on('data', (data) => {
  process.stderr.write(data.toString())
})

// ── Step 3: Launch Electron with dev URL ──────────────────────────
function launchElectron() {
  const env = {
    ...process.env,
    GPTBRIDGE_RENDERER_DEV_URL: RENDERER_DEV_URL,
    GPTBRIDGE_MANAGE_BACKEND: '1',
  }
  const electron = spawn(
    process.platform === 'win32' ? 'npx.cmd' : 'npx',
    ['electron', '.'],
    { cwd: root, stdio: 'inherit', env }
  )

  electron.on('close', (code) => {
    console.log(`[dev] Electron exited with code ${code}`)
    vite.kill()
    process.exit(code ?? 0)
  })
}

// Safety timeout: if Vite doesn't start in 15s, exit
setTimeout(() => {
  if (!viteReady) {
    console.error('[dev] Vite dev server failed to start within 15s')
    vite.kill()
    process.exit(1)
  }
}, 15000)

// Clean up on exit
process.on('SIGINT', () => {
  vite.kill()
  process.exit(0)
})
process.on('SIGTERM', () => {
  vite.kill()
  process.exit(0)
})
