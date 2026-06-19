import { app, BrowserWindow, dialog, ipcMain, shell } from 'electron'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { AdaptiveZoomController } from './adaptiveZoom'
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

let mainWindow: BrowserWindow | null = null
const toolWindows = new Map<string, BrowserWindow>()
let quitting = false
let lastCpuSnapshot: { idle: number; total: number } | null = null
let currentUiZoom = 1
const sourceProduction = getRuntimeEnv('GPTBRIDGE_SOURCE_PRODUCTION') === '1'

if (sourceProduction) {
  app.setName('程式庫')
  app.setPath(
    'userData',
    path.join(app.getPath('appData'), 'gptbridge-auto-agent-ide')
  )
}

const usesBuiltRenderer = app.isPackaged || sourceProduction
const shouldManageBackend =
  getRuntimeEnv('GPTBRIDGE_MANAGE_BACKEND') === '1' ||
  sourceProduction ||
  (app.isPackaged && getRuntimeEnv('GPTBRIDGE_MANAGE_BACKEND') !== '0')

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

type ToolWindowConfig = {
  width: number
  height: number
  minWidth: number
  minHeight: number
  title?: string
}

const DEFAULT_TOOL_WINDOW_CONFIG: ToolWindowConfig = {
  width: 860,
  height: 680,
  minWidth: 760,
  minHeight: 560,
}

const TOOL_ID_PATTERN = /^[a-z0-9_-]+$/
function readJsonFile(filePath: string): Record<string, unknown> | null {
  try {
    return JSON.parse(fs.readFileSync(filePath, 'utf-8')) as Record<string, unknown>
  } catch {
    return null
  }
}

function resolvePlatformToolsLocation(): {
  platformToolsPath: string
  searchedPaths: string[]
} {
  const paths = getRuntimePathLibrary()
  const searchedPaths = Array.from(
    new Set(
      [
        path.join(paths.workspaceRoot, 'platform_tools'),
        path.join(paths.resourcesRoot, 'platform_tools'),
        path.join(paths.appRoot, 'platform_tools'),
        path.join(paths.unpackedRoot, 'platform_tools'),
        path.join(paths.executableDir, 'resources', 'platform_tools'),
        path.join(process.cwd(), 'platform_tools'),
      ].map((candidate) => path.resolve(candidate))
    )
  )
  const platformToolsPath =
    searchedPaths.find((candidate) => fs.existsSync(candidate)) ?? searchedPaths[0]
  return { platformToolsPath, searchedPaths }
}

function directorySizeBytes(rootPath: string): number {
  let total = 0

  const visit = (currentPath: string): void => {
    let entries: fs.Dirent[]
    try {
      entries = fs.readdirSync(currentPath, { withFileTypes: true })
    } catch {
      return
    }

    for (const entry of entries) {
      const entryPath = path.join(currentPath, entry.name)
      if (entry.isDirectory()) {
        visit(entryPath)
        continue
      }

      if (!entry.isFile()) continue

      try {
        total += fs.statSync(entryPath).size
      } catch {
        // Ignore files that disappear or are locked while scanning.
      }
    }
  }

  visit(rootPath)
  return total
}

function readPlatformToolSizes(): {
  ok: boolean
  tools: Array<{
    id: string
    folder_path: string
    manifest_path: string
    project_size_bytes: number
  }>
  platform_tools_path: string
  searched_paths: string[]
} {
  const { platformToolsPath, searchedPaths } = resolvePlatformToolsLocation()
  const tools: Array<{
    id: string
    folder_path: string
    manifest_path: string
    project_size_bytes: number
  }> = []

  if (!fs.existsSync(platformToolsPath)) {
    return {
      ok: true,
      tools,
      platform_tools_path: platformToolsPath,
      searched_paths: searchedPaths,
    }
  }

  for (const entry of fs.readdirSync(platformToolsPath, { withFileTypes: true })) {
    if (!entry.isDirectory() || !TOOL_ID_PATTERN.test(entry.name)) continue
    const folderPath = path.join(platformToolsPath, entry.name)
    const manifestPath = path.join(folderPath, 'manifest.json')
    if (!fs.existsSync(manifestPath)) continue

    tools.push({
      id: entry.name,
      folder_path: folderPath,
      manifest_path: manifestPath,
      project_size_bytes: directorySizeBytes(folderPath),
    })
  }

  return {
    ok: true,
    tools,
    platform_tools_path: platformToolsPath,
    searched_paths: searchedPaths,
  }
}

function positiveInteger(value: unknown): number | undefined {
  if (typeof value !== 'number' || !Number.isFinite(value)) return undefined
  const normalized = Math.floor(value)
  return normalized > 0 ? normalized : undefined
}

function readPlatformToolWindowConfig(toolId: string): Partial<ToolWindowConfig> {
  if (!TOOL_ID_PATTERN.test(toolId)) return {}

  const { platformToolsPath } = resolvePlatformToolsLocation()
  const manifest = readJsonFile(
    path.join(platformToolsPath, toolId, 'manifest.json')
  )
  if (!manifest) return {}

  const windowConfig =
    typeof manifest.window === 'object' && manifest.window !== null
      ? (manifest.window as Record<string, unknown>)
      : {}

  const config: Partial<ToolWindowConfig> = {}
  const width = positiveInteger(windowConfig.width)
  const height = positiveInteger(windowConfig.height)
  const minWidth = positiveInteger(windowConfig.minWidth)
  const minHeight = positiveInteger(windowConfig.minHeight)
  if (width !== undefined) config.width = width
  if (height !== undefined) config.height = height
  if (minWidth !== undefined) config.minWidth = minWidth
  if (minHeight !== undefined) config.minHeight = minHeight
  if (typeof windowConfig.title === 'string') {
    config.title = windowConfig.title
  } else if (typeof manifest.name === 'string') {
    config.title = manifest.name
  }
  return config
}

function clampUiZoom(value: number): number {
  return Math.max(MIN_UI_ZOOM, Math.min(MAX_UI_ZOOM, value))
}

function writeRuntimeLog(
  event: string,
  payload: Record<string, unknown> = {}
): void {
  try {
    const logsRoot = path.join(app.getPath('userData'), 'logs')
    fs.mkdirSync(logsRoot, { recursive: true })
    fs.appendFileSync(
      path.join(logsRoot, 'exe-runtime.log'),
      `[${new Date().toISOString()}] ${event} ${JSON.stringify(payload)}\n`,
      'utf-8'
    )
  } catch {
    // Logging must never block startup.
  }
}

process.on('uncaughtException', (error) => {
  writeRuntimeLog('main.uncaughtException', {
    message: error.message,
    stack: error.stack,
  })
})

process.on('unhandledRejection', (reason) => {
  writeRuntimeLog('main.unhandledRejection', {
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

function getSystemMetrics() {
  const totalMemBytes = os.totalmem()
  const freeMemBytes = os.freemem()
  const ramUsagePercent =
    totalMemBytes > 0
      ? ((totalMemBytes - freeMemBytes) / totalMemBytes) * 100
      : 0

  const paths = getRuntimePathLibrary()
  writeRuntimeLog('window.create.start', {
    isPackaged: app.isPackaged,
    sourceProduction,
    shouldManageBackend,
    workspaceRoot: paths.workspaceRoot,
    resourcesRoot: paths.resourcesRoot,
    rendererEntryHtml: paths.rendererEntryHtml,
    pythonExecutable: paths.pythonExecutable,
    pythonEntry: paths.pythonEntry,
  })
  const disk =
    readDiskMetrics(paths.workspaceRoot) ?? readDiskMetrics(process.cwd())

  return {
    cpuUsagePercent: readCpuUsagePercent(),
    ramUsagePercent: Math.max(0, Math.min(100, ramUsagePercent)),
    ramTotalBytes: totalMemBytes,
    ramFreeBytes: freeMemBytes,
    diskUsagePercent: disk?.usagePercent ?? null,
    diskTotalBytes: disk?.totalBytes ?? null,
    diskFreeBytes: disk?.freeBytes ?? null,
    sampledAt: Date.now(),
  }
}

function resolveRendererUrl(): string {
  return getRuntimeEnv('VITE_DEV_SERVER_URL') || 'http://127.0.0.1:5180/'
}

function buildToolWindowUrl(toolId: string): string {
  const url = new URL(resolveRendererUrl())
  url.searchParams.set('toolWindow', '1')
  url.searchParams.set('tool', toolId)
  return url.toString()
}

async function createWindow(): Promise<void> {
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.focus()
    return
  }

  const paths = getRuntimePathLibrary()

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
      sandbox: false,
      preload: paths.preloadEntry,
    },
  })

  adaptiveZoomController.register(mainWindow)

  mainWindow.once('ready-to-show', () => {
    mainWindow?.show()
    mainWindow?.focus()
  })

  mainWindow.on('closed', () => {
    mainWindow = null
  })

  mainWindow.webContents.on('did-fail-load', (_event, code, desc) => {
    console.error('[BOOT] Renderer load failed:', code, desc)
    writeRuntimeLog('window.renderer.did-fail-load', { code, desc })
  })

  if (getRuntimeEnv('GPTBRIDGE_OPEN_DEVTOOLS') === '1') {
    mainWindow.webContents.openDevTools({ mode: 'detach' })
  }

  if (usesBuiltRenderer) {
    await mainWindow.loadFile(paths.rendererEntryHtml)
    writeRuntimeLog('window.load-file.ok', {
      rendererEntryHtml: paths.rendererEntryHtml,
    })
    return
  }

  const url = resolveRendererUrl()
  let attempts = 0
  while (attempts < 20) {
    try {
      await mainWindow.loadURL(url)
      writeRuntimeLog('window.load-url.ok', { url })
      return
    } catch (error) {
      attempts += 1
      await new Promise((resolve) => setTimeout(resolve, 500))
      if (!mainWindow || mainWindow.isDestroyed()) return
      if (attempts === 20) throw error
    }
  }
}

async function createToolWindow(
  toolId: string
): Promise<{ ok: boolean; focused?: boolean; message?: string }> {
  if (!TOOL_ID_PATTERN.test(toolId)) {
    return { ok: false, message: `Invalid tool: ${toolId}` }
  }

  writeRuntimeLog('tool-window.removed', { toolId })
  return {
    ok: false,
    message:
      'Platform applications now run as standalone EXEs. Use toolbox_start_tool through the backend manager.',
  }
}

function registerIpcHandlers(): void {
  ipcMain.handle('app:get-status', async () => {
    const backendRuntime = getBackendRuntimeInfo()

    return {
      isPackaged: app.isPackaged,
      version: app.getVersion(),
      backendStatus: backendRuntime.status,
      backendReady: backendRuntime.ready,
      backendStartupMs: backendRuntime.startupMs,
      backendMessage: backendRuntime.message,
      environment: getRuntimeEnv('NODE_ENV') || 'development',
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

    const backendStatus = ensureBackendStarted()
    return {
      ok: backendStatus !== 'error',
      managed: true,
      backendStatus,
    }
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

  ipcMain.handle(
    'app:open-tool-window',
    async (_event, payload: { toolId?: string }) => {
      const toolId = String(payload?.toolId || '').trim()
      return createToolWindow(toolId)
    }
  )

  ipcMain.handle(
    'app:close-tool-window',
    async (_event, payload: { toolId?: string }) => {
      const toolId = String(payload?.toolId || '').trim()
      const current = toolWindows.get(toolId)
      if (!current || current.isDestroyed())
        return { ok: true, alreadyClosed: true }
      current.close()
      return { ok: true }
    }
  )

  ipcMain.handle('app:get-open-tool-windows', async () => {
    const toolIds = Array.from(toolWindows.entries())
      .filter(([, toolWindow]) => !toolWindow.isDestroyed())
      .map(([toolId]) => toolId)
    return { ok: true, toolIds }
  })

  ipcMain.handle('app:get-platform-tool-sizes', async () => {
    return readPlatformToolSizes()
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
        { name: 'Portfolio Data', extensions: ['csv', 'tsv', 'json', 'xlsx', 'xls'] },
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
  app.quit()
} else {
  app.on('second-instance', () => {
    if (!mainWindow || mainWindow.isDestroyed()) {
      void createWindow()
      return
    }
    if (mainWindow.isMinimized()) mainWindow.restore()
    mainWindow.show()
    mainWindow.focus()
  })

  app.whenReady().then(async () => {
    try {
      writeRuntimeLog('bootstrap.start', {
        isPackaged: app.isPackaged,
        sourceProduction,
        shouldManageBackend,
        cwd: process.cwd(),
        userData: app.getPath('userData'),
      })

      registerIpcHandlers()

      if (shouldManageBackend) {
        startBackend()
      }

      await createWindow()
      writeRuntimeLog('bootstrap.ready')

      app.on('activate', () => {
        if (BrowserWindow.getAllWindows().length === 0) {
          void createWindow()
        }
      })
    } catch (error) {
      writeRuntimeLog('bootstrap.failed', {
        message: error instanceof Error ? error.message : String(error),
        stack: error instanceof Error ? error.stack : undefined,
      })
      throw error
    }
  })
}

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') {
    app.quit()
  }
})

app.on('before-quit', (event) => {
  if (quitting || !shouldManageBackend) return

  event.preventDefault()
  quitting = true
  stopBackend()
    .catch((error: unknown) => {
      console.error('[BOOT] Backend shutdown failed:', error)
    })
    .finally(() => {
      app.quit()
    })
})
