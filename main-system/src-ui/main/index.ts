import { app, BrowserWindow, dialog, ipcMain, shell } from 'electron'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { AdaptiveZoomController } from './adaptiveZoom'
import { getBackendSessionDescriptor } from './ipcSession'
import { getRuntimePathLibrary } from './pathLibrary'
import {
  ensureBackendStarted,
  getBackendRuntimeInfo,
  getBackendStatus,
  restartBackend,
  startBackend,
  stopBackend,
} from './python-backend'
import { getRuntimeEnv } from './runtime-env'
import { PRODUCT_VERSION } from './product-version'

import {
  registerEmbeddedBrowser,
  registerEmbeddedBrowserIpc,
  closeAllSessions,
} from './embedded-browser'
import {
  getMainSystemSize,
  getPlatformToolSizes,
  getSharedLayerSize,
  getWorkspaceSize,
} from './platform-tool-sizes'

let mainWindow: BrowserWindow | null = null
let lastCpuSnapshot: { idle: number; total: number } | null = null
let currentUiZoom = 1
let mainRendererReloadTimer: NodeJS.Timeout | null = null
const sourceProduction = !app.isPackaged

if (sourceProduction) {
  app.setName('GPTBridge')
  app.setPath(
    'userData',
    path.join(app.getPath('appData'), 'gptbridge-auto-agent-ide')
  )
}

const shouldManageBackend = getRuntimeEnv('GPTBRIDGE_MANAGE_BACKEND') === '1'

const MIN_UI_ZOOM = 0.85
const MAX_UI_ZOOM = 1.3
const adaptiveZoomController = new AdaptiveZoomController(() => currentUiZoom)
const isPathInside = (basePath: string, targetPath: string) => {
  const relative = path.relative(basePath, targetPath)
  return (
    relative === '' ||
    (!!relative && !relative.startsWith('..') && !path.isAbsolute(relative))
  )
}

function clampUiZoom(value: number): number {
  return Math.max(MIN_UI_ZOOM, Math.min(MAX_UI_ZOOM, value))
}

function reportRuntimeEvent(
  event: string,
  payload: Record<string, unknown> = {}
): void {
  console.info(`[Main System] ${event}`, payload)
}

process.on('uncaughtException', (error) => {
  reportRuntimeEvent('main.uncaughtException', {
    message: error.message,
    stack: error.stack,
  })
})

process.on('unhandledRejection', (reason) => {
  reportRuntimeEvent('main.unhandledRejection', {
    message: reason instanceof Error ? reason.message : String(reason),
    stack: reason instanceof Error ? reason.stack : undefined,
  })
})

function readCpuSnapshot(): { idle: number; total: number } {
  let idle = 0
  let total = 0
  for (const cpu of os.cpus()) {
    idle += cpu.times.idle
    total +=
      cpu.times.user +
      cpu.times.nice +
      cpu.times.sys +
      cpu.times.irq +
      cpu.times.idle
  }
  return { idle, total }
}

function readCpuUsagePercent(): number | null {
  const current = readCpuSnapshot()
  if (!lastCpuSnapshot) {
    lastCpuSnapshot = current
    return null
  }

  const totalDiff = current.total - lastCpuSnapshot.total
  const idleDiff = current.idle - lastCpuSnapshot.idle
  lastCpuSnapshot = current

  if (totalDiff <= 0) return null
  const usage = (1 - idleDiff / totalDiff) * 100
  return Math.max(0, Math.min(100, usage))
}

function readDiskMetrics(
  rootPath: string
): { totalBytes: number; freeBytes: number; usagePercent: number } | null {
  try {
    const stats = fs.statfsSync(rootPath)
    const blockSize = Number((stats as any).bsize ?? 0)
    const totalBlocks = Number((stats as any).blocks ?? 0)
    const freeBlocks = Number(
      (stats as any).bavail ?? (stats as any).bfree ?? 0
    )

    if (blockSize <= 0 || totalBlocks <= 0) return null

    const totalBytes = blockSize * totalBlocks
    const freeBytes = blockSize * freeBlocks
    const usagePercent = ((totalBytes - freeBytes) / totalBytes) * 100

    return {
      totalBytes,
      freeBytes,
      usagePercent: Math.max(0, Math.min(100, usagePercent)),
    }
  } catch {
    return null
  }
}

function resolveSystemDiskRoot(): string {
  if (process.platform !== 'win32') return path.parse(os.homedir()).root || '/'

  const configuredDrive = (getRuntimeEnv('SystemDrive') || '').trim()
  if (/^[a-z]:$/i.test(configuredDrive)) return `${configuredDrive}\\`
  if (configuredDrive) return path.parse(path.resolve(configuredDrive)).root
  return path.parse(os.homedir()).root || path.parse(process.cwd()).root
}

function getSystemMetrics() {
  const totalMemBytes = os.totalmem()
  const freeMemBytes = os.freemem()
  const ramUsagePercent =
    totalMemBytes > 0
      ? ((totalMemBytes - freeMemBytes) / totalMemBytes) * 100
      : 0

  const diskRoot = resolveSystemDiskRoot()
  const disk = readDiskMetrics(diskRoot)

  return {
    cpuUsagePercent: readCpuUsagePercent(),
    ramUsagePercent: Math.max(0, Math.min(100, ramUsagePercent)),
    ramTotalBytes: totalMemBytes,
    ramFreeBytes: freeMemBytes,
    diskUsagePercent: disk?.usagePercent ?? null,
    diskTotalBytes: disk?.totalBytes ?? null,
    diskFreeBytes: disk?.freeBytes ?? null,
    diskRoot,
    sampledAt: Date.now(),
  }
}

async function createWindow(): Promise<void> {
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.focus()
    return
  }

  const paths = getRuntimePathLibrary()
  reportRuntimeEvent('window.create.start', {
    isPackaged: app.isPackaged,
    sourceProduction,
    shouldManageBackend,
    workspaceRoot: paths.workspaceRoot,
    resourcesRoot: paths.resourcesRoot,
    rendererEntryHtml: paths.rendererEntryHtml,
    pythonExecutable: paths.pythonExecutable,
    pythonEntry: paths.pythonEntry,
  })

  mainWindow = new BrowserWindow({
    width: 1400,
    height: 900,
    minWidth: 1100,
    minHeight: 720,
    show: false,
    backgroundColor: '#1a1b1e',
    titleBarStyle: 'hidden',
    titleBarOverlay:
      process.platform === 'win32'
        ? {
            color: '#1a1b1e',
            symbolColor: '#ffffff',
            height: 35,
          }
        : false,
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      preload: paths.preloadEntry,
    },
  })

  adaptiveZoomController.register(mainWindow)

  // Register embedded browser so tools can use in-app BrowserView
  // instead of launching external Chrome/Edge (A44/E30 + A49/E35).
  registerEmbeddedBrowser(mainWindow)

  mainWindow.webContents.on('before-input-event', (event, input) => {
    if (input.type !== 'keyDown') return
    const key = input.key.toLowerCase()
    const reloadShortcut = key === 'f5' || ((input.control || input.meta) && key === 'r')
    if (!reloadShortcut) return
    event.preventDefault()
    if (input.shift) {
      mainWindow?.webContents.reloadIgnoringCache()
    } else {
      mainWindow?.webContents.reload()
    }
  })

  mainWindow.once('ready-to-show', () => {
    mainWindow?.show()
    mainWindow?.focus()
  })

  mainWindow.on('closed', () => {
    mainWindow = null
  })

  mainWindow.webContents.on('did-fail-load', (_event, code, desc) => {
    console.error('[BOOT] Renderer load failed:', code, desc)
    reportRuntimeEvent('window.renderer.did-fail-load', { code, desc })
  })

  // Dev mode: load from Vite dev server (no build needed, HMR active).
  // Production mode: load built assets from dist-ui/renderer/index.html.
  const devServerUrl = getRuntimeEnv('GPTBRIDGE_RENDERER_DEV_URL')
  if (devServerUrl) {
    await mainWindow.loadURL(devServerUrl)
    mainWindow.webContents.openDevTools({ mode: 'detach' })
    reportRuntimeEvent('window.load-url.ok', { devServerUrl })
  } else {
    await mainWindow.loadFile(paths.rendererEntryHtml)
    reportRuntimeEvent('window.load-file.ok', {
      rendererEntryHtml: paths.rendererEntryHtml,
    })
  }
}

function startMainRendererWatch() {
  const paths = getRuntimePathLibrary()
  if (!paths.rendererEntryHtml || !fs.existsSync(paths.rendererEntryHtml)) return
  fs.watchFile(paths.rendererEntryHtml, { interval: 500 }, () => {
    if (!mainWindow || mainWindow.isDestroyed()) return
    if (mainRendererReloadTimer) {
      clearTimeout(mainRendererReloadTimer)
      mainRendererReloadTimer = null
    }
    mainRendererReloadTimer = setTimeout(() => {
      if (!mainWindow || mainWindow.isDestroyed()) return
      mainWindow.webContents.reloadIgnoringCache()
    }, 500)
  })
}

function stopMainRendererWatch() {
  if (mainRendererReloadTimer) {
    clearTimeout(mainRendererReloadTimer)
    mainRendererReloadTimer = null
  }
  const paths = getRuntimePathLibrary()
  if (paths.rendererEntryHtml) {
    fs.unwatchFile(paths.rendererEntryHtml)
  }
}

function registerIpcHandlers(): void {
  // Embedded browser IPC (replaces external Playwright/Chrome/Edge)
  registerEmbeddedBrowserIpc()

  ipcMain.handle('app:get-status', async () => {
    const backendRuntime = getBackendRuntimeInfo()

    return {
      isPackaged: app.isPackaged,
      version: PRODUCT_VERSION,
      backendStatus: backendRuntime.status,
      backendManaged: shouldManageBackend,
      backendReady: backendRuntime.ready,
      backendStartupMs: backendRuntime.startupMs,
      backendMessage: backendRuntime.message,
      environment: app.isPackaged || sourceProduction ? 'production' : 'source',
      sourceProduction,
      systemReady: shouldManageBackend
        ? backendRuntime.ready
        : backendRuntime.status !== 'error',
      bootTimestamp: Date.now(),
      systemMetrics: getSystemMetrics(),
    }
  })

  ipcMain.handle('app:ensure-backend-started', async () => {
    if (!shouldManageBackend) {
      return {
        ok: false,
        managed: false,
        backendStatus: getBackendStatus(),
        message: 'backend manager is disabled by GPTBRIDGE_MANAGE_BACKEND=0',
      }
    }

    const backendStatus = await ensureBackendStarted()
    return {
      ok: backendStatus !== 'error',
      managed: true,
      backendStatus,
    }
  })

  ipcMain.handle('app:get-backend-session', async () => {
    return getBackendSessionDescriptor()
  })

  ipcMain.handle('app:restart-backend', async () => {
    if (!shouldManageBackend) {
      return {
        ok: false,
        managed: false,
        backendStatus: getBackendStatus(),
        message: '後端目前由外部 dev 腳本管理，無法由 Electron 單獨重啟。',
      }
    }

    const backendStatus = await restartBackend()
    return {
      ok: backendStatus !== 'error',
      managed: true,
      backendStatus,
    }
  })

  ipcMain.handle('app:restart', async () => {
    app.relaunch()
    app.quit()
  })

  ipcMain.handle('app:get-platform-tool-sizes', async (_event, payload?: unknown) => {
    const forceRefresh = Boolean(
      payload &&
      typeof payload === 'object' &&
      (payload as { forceRefresh?: unknown }).forceRefresh === true
    )
    const paths = getRuntimePathLibrary()
    const tools = await getPlatformToolSizes(paths.workspaceRoot, forceRefresh)
    const mainSystem = await getMainSystemSize(paths.workspaceRoot, forceRefresh)
    const sharedLayer = await getSharedLayerSize(paths.workspaceRoot, forceRefresh)
    const workspace = await getWorkspaceSize(
      paths.workspaceRoot,
      tools,
      mainSystem,
      sharedLayer,
      forceRefresh
    )
    return {
      ok: true,
      tools,
      main_system: mainSystem,
      shared_layer: sharedLayer,
      workspace,
      source: 'governed-local-folder-inventory',
    }
  })

  ipcMain.handle('app:reload-window', async () => {
    if (!mainWindow || mainWindow.isDestroyed()) return { ok: false }
    mainWindow.reload()
    return { ok: true }
  })

  ipcMain.handle('app:reload-window-hard', async () => {
    if (!mainWindow || mainWindow.isDestroyed()) return { ok: false }
    mainWindow.webContents.reloadIgnoringCache()
    return { ok: true }
  })

  ipcMain.handle('app:get-ui-zoom', async () => {
    return { ok: true, factor: currentUiZoom }
  })

  ipcMain.handle(
    'app:set-ui-zoom',
    async (_event, payload: { factor?: number }) => {
      if (
        BrowserWindow.getAllWindows().every((window) => window.isDestroyed())
      ) {
        return { ok: false, message: 'Window not ready' }
      }

      const target = clampUiZoom(Number(payload?.factor ?? 1))
      if (Number.isNaN(target) || target <= 0) {
        return { ok: false, message: 'Invalid zoom factor' }
      }

      currentUiZoom = target
      adaptiveZoomController.applyAll()
      return { ok: true, factor: currentUiZoom }
    }
  )

  ipcMain.handle(
    'app:open-path',
    async (
      _event,
      payload: {
        path?: string
        basePath?: string
        relativePath?: string
        mode?: 'open' | 'reveal'
      }
    ) => {
      const rawPath = String(payload?.path || '').trim()
      const basePath = String(payload?.basePath || '').trim()
      const relativePath = String(payload?.relativePath || '').trim()
      const mode = payload?.mode === 'reveal' ? 'reveal' : 'open'

      const workspaceRoot = path.resolve(getRuntimePathLibrary().workspaceRoot)
      let targetPath = rawPath ? path.resolve(rawPath) : ''
      if (basePath && relativePath) {
        const resolvedBase = path.resolve(basePath)
        const resolvedTarget = path.resolve(resolvedBase, relativePath)
        if (!isPathInside(resolvedBase, resolvedTarget)) {
          return { ok: false, message: 'Path is outside the selected folder' }
        }
        targetPath = resolvedTarget
      }

      if (!targetPath) return { ok: false, message: 'Missing path' }
      if (!isPathInside(workspaceRoot, targetPath)) {
        return { ok: false, message: 'Path is outside the project workspace' }
      }
      if (!fs.existsSync(targetPath)) {
        return { ok: false, message: 'File no longer exists' }
      }

      if (mode === 'reveal') {
        shell.showItemInFolder(targetPath)
        return { ok: true }
      }

      const errorMessage = await shell.openPath(targetPath)
      return errorMessage ? { ok: false, message: errorMessage } : { ok: true }
    }
  )

  ipcMain.handle('dialog:select-folder', async (event) => {
    const window =
      BrowserWindow.fromWebContents(event.sender) ?? mainWindow ?? undefined
    const result = window
      ? await dialog.showOpenDialog(window, {
          properties: ['openDirectory', 'createDirectory'],
        })
      : await dialog.showOpenDialog({
          properties: ['openDirectory', 'createDirectory'],
        })

    if (result.canceled || result.filePaths.length === 0) return ''
    return result.filePaths[0]
  })

  ipcMain.handle('dialog:create-file', async (event, defaultPath: string) => {
    const window =
      BrowserWindow.fromWebContents(event.sender) ?? mainWindow ?? undefined
    const options: Electron.SaveDialogOptions = {
      defaultPath,
      filters: [
        { name: 'Code', extensions: ['py', 'json', 'md', 'txt'] },
        { name: 'All Files', extensions: ['*'] },
      ],
    }

    const result = window
      ? await dialog.showSaveDialog(window, options)
      : await dialog.showSaveDialog(options)

    if (result.canceled || !result.filePath) return ''
    return result.filePath
  })

  ipcMain.handle('dialog:open-file', async (event, defaultPath: string) => {
    const window =
      BrowserWindow.fromWebContents(event.sender) ?? mainWindow ?? undefined
    const options: Electron.OpenDialogOptions = {
      defaultPath,
      properties: ['openFile'],
      filters: [
        { name: 'Structured Data', extensions: ['csv', 'tsv', 'json', 'xlsx', 'xls'] },
        { name: 'Code', extensions: ['py', 'json', 'md', 'txt'] },
        { name: 'All Files', extensions: ['*'] },
      ],
    }

    const result = window
      ? await dialog.showOpenDialog(window, options)
      : await dialog.showOpenDialog(options)

    if (result.canceled || result.filePaths.length === 0) return ''
    return result.filePaths[0]
  })
}

const hasSingleInstanceLock = app.requestSingleInstanceLock()

if (!hasSingleInstanceLock) {
  // Another instance already holds the lock. Exit immediately without
  // waiting for the ready event — app.quit() may not fire before-quit
  // handlers when the app hasn't finished initializing yet.
  reportRuntimeEvent('main.single-instance.exiting')
  app.exit(0)
} else {
  app.on('second-instance', () => {
    if (!mainWindow || mainWindow.isDestroyed()) {
      void createWindow()
      return
    }
    // Focus the existing window instead of reloading it, so the user's
    // current state is preserved.
    if (mainWindow.isMinimized()) mainWindow.restore()
    if (!mainWindow.isVisible()) mainWindow.show()
    mainWindow.focus()
  })

  app.whenReady().then(async () => {
    try {
      reportRuntimeEvent('bootstrap.start', {
        isPackaged: app.isPackaged,
        sourceProduction,
        shouldManageBackend,
        cwd: process.cwd(),
        userData: app.getPath('userData'),
      })

      registerIpcHandlers()

      // Show the window FIRST so the startup page appears immediately.
      // Backend startup (boot_core spawn) runs in the background and does
      // not block the UI.  A60: the launcher does NOT generate governance
      // bootstrap material — boot_core generates its own fresh token per
      // spawn.  The launcher only spawns boot_core and tracks its liveness.
      await createWindow()
      startMainRendererWatch()
      reportRuntimeEvent('window.ready')

      // Start the backend in the background.  A60: the launcher only spawns
      // boot_core; boot_core independently performs environment check,
      // governance audit, dependency start, governance-system start, and
      // system-core start per A61 ordering.
      if (shouldManageBackend) {
        try {
          void startBackend()
        } catch (error) {
          reportRuntimeEvent('backend.start.failed', {
            message: error instanceof Error ? error.message : String(error),
          })
        }
      }



      reportRuntimeEvent('bootstrap.ready')

      app.on('activate', () => {
        if (BrowserWindow.getAllWindows().length === 0) {
          void createWindow()
        }
      })
    } catch (error) {
      reportRuntimeEvent('bootstrap.failed', {
        message: error instanceof Error ? error.message : String(error),
        stack: error instanceof Error ? error.stack : undefined,
      })
      // Do not rethrow: the startup entry's only hard requirement is to show
      // the page; any remaining errors are reported and tolerated.
    }
  })
}

// In-place relaunch: the dev watcher sends SIGUSR2 after a main rebuild.
// app.relaunch() reuses the same process tree + env, then app.exit() lets
// the watcher spawn the fresh bundle without a visible "close + reopen".
if (process.env.GPTBRIDGE_RENDERER_DEV_URL) {
  process.on('SIGUSR2', () => {
    reportRuntimeEvent('main.relaunch.requested')
    app.relaunch()
    app.exit(0)
  })
}

// Complete-close contract: closing the last window fully terminates the
// application — embedded sessions, renderer watchers, the managed backend
// (boot_core + main.py), and the Electron process itself.  The path is
// idempotent: window-all-closed, before-quit, and repeated quit attempts
// all converge on a single shared shutdown.
let shutdownPromise: Promise<void> | null = null
const SHUTDOWN_DEADLINE_MS = 15_000

function shutdownApplication(): void {
  if (shutdownPromise) return
  shutdownPromise = (async () => {
    closeAllSessions()
    stopMainRendererWatch()
    if (shouldManageBackend) {
      // Backend shutdown is awaited but bounded — a stalled graceful stop
      // must never leave the UI running as a detached orphan.
      await Promise.race([
        stopBackend(),
        new Promise<void>((resolve) =>
          setTimeout(resolve, SHUTDOWN_DEADLINE_MS)
        ),
      ])
    }
  })()
    .catch(() => {
      // Best-effort cleanup; nothing may block the final exit.
    })
    .finally(() => {
      reportRuntimeEvent('main.ui-shutdown')
      app.exit(0)
    })
}

app.on('window-all-closed', () => {
  // Closing the last window closes the whole application on every
  // platform — no dock-resident or detached backend remains.
  shutdownApplication()
})

app.on('before-quit', (event) => {
  // Single exit path: hold the quit until the shared shutdown finishes
  // and calls app.exit(0).  preventDefault is unconditional so re-entrant
  // quit events can never race the cleanup in flight.
  event.preventDefault()
  shutdownApplication()
})
