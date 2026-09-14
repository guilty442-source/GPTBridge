import { app, BrowserWindow, ipcMain, shell } from 'electron'
import fs from 'node:fs'
import path from 'node:path'
import { AdaptiveZoomController } from './adaptiveZoom'
import { getRuntimePathLibrary } from './pathLibrary'
import {
  startBackend,
  stopBackend,
} from './python-backend'
import { getRuntimeEnv } from './runtime-env'

import {
  registerEmbeddedBrowser,
  closeAllSessions,
} from './embedded-browser'
import {
  startEmbeddedBrowserBridge,
  stopEmbeddedBrowserBridge,
} from './embedded-browser-bridge'
import { registerIpcHandlers } from './ipcHandlers'

let mainWindow: BrowserWindow | null = null
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

const adaptiveZoomController = new AdaptiveZoomController(() => currentUiZoom)
const isPathInside = (basePath: string, targetPath: string) => {
  const relative = path.relative(basePath, targetPath)
  return (
    relative === '' ||
    (!!relative && !relative.startsWith('..') && !path.isAbsolute(relative))
  )
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
  // Publish the loopback bridge so tool UIs and tool Python backends reach
  // the same embedded-browser session store without a second browser stack.
  startEmbeddedBrowserBridge()

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
  const watchedEntries = [paths.rendererEntryHtml, paths.preloadEntry].filter(
    (entry): entry is string => Boolean(entry) && fs.existsSync(entry)
  )
  if (watchedEntries.length === 0) return
  // A rebuilt renderer entry or preload must apply without restarting the
  // app: reload the window (preload re-runs on every load) with a short
  // debounce so a multi-file build lands as one reload.
  const scheduleReload = () => {
    if (!mainWindow || mainWindow.isDestroyed()) return
    if (mainRendererReloadTimer) {
      clearTimeout(mainRendererReloadTimer)
      mainRendererReloadTimer = null
    }
    mainRendererReloadTimer = setTimeout(() => {
      if (!mainWindow || mainWindow.isDestroyed()) return
      mainWindow.webContents.reloadIgnoringCache()
      reportRuntimeEvent('renderer.hot-reload', {
        rendererEntryHtml: paths.rendererEntryHtml,
      })
    }, 500)
  }
  for (const entry of watchedEntries) {
    fs.watchFile(entry, { interval: 500 }, scheduleReload)
  }
}

function stopMainRendererWatch() {
  if (mainRendererReloadTimer) {
    clearTimeout(mainRendererReloadTimer)
    mainRendererReloadTimer = null
  }
  const paths = getRuntimePathLibrary()
  for (const entry of [paths.rendererEntryHtml, paths.preloadEntry]) {
    if (entry) {
      fs.unwatchFile(entry)
    }
  }
}

const hasSingleInstanceLock = app.requestSingleInstanceLock()

if (!hasSingleInstanceLock) {
  // Another instance already holds the lock. Exit immediately without
  // waiting for the ready event ?'app.quit() may not fire before-quit
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

      registerIpcHandlers(() => mainWindow)

      // Show the window FIRST so the startup page appears immediately.
      // Backend startup (boot_core spawn) runs in the background and does
      // not block the UI.  A60: the launcher does NOT generate governance
      // bootstrap material ?'boot_core generates its own fresh token per
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
// application ?'embedded sessions, renderer watchers, the managed backend
// (boot_core + main.py), and the Electron process itself.  The path is
// idempotent: window-all-closed, before-quit, and repeated quit attempts
// all converge on a single shared shutdown.
let shutdownPromise: Promise<void> | null = null
const SHUTDOWN_DEADLINE_MS = 15_000

function shutdownApplication(): void {
  if (shutdownPromise) return
  shutdownPromise = (async () => {
    closeAllSessions()
    stopEmbeddedBrowserBridge()
    stopMainRendererWatch()
    if (shouldManageBackend) {
      // Backend shutdown is awaited but bounded ?'a stalled graceful stop
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
  // platform ?'no dock-resident or detached backend remains.
  shutdownApplication()
})

app.on('before-quit', (event) => {
  // Single exit path: hold the quit until the shared shutdown finishes
  // and calls app.exit(0).  preventDefault is unconditional so re-entrant
  // quit events can never race the cleanup in flight.
  event.preventDefault()
  shutdownApplication()
})
