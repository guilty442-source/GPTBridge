import { app, BrowserWindow, dialog, ipcMain, shell } from 'electron'
import fs from 'node:fs'
import path from 'node:path'

import { getBackendSessionDescriptor } from './ipcSession'
import { registerEmbeddedBrowserIpc } from './embedded-browser'
import { getRuntimePathLibrary } from './pathLibrary'
import {
  ensureBackendStarted,
  getBackendRuntimeInfo,
  getBackendStatus,
  restartBackend,
} from './python-backend'
import { getRuntimeEnv } from './runtime-env'
import { PRODUCT_VERSION } from './product-version'
import {
  getMainSystemSize,
  getPlatformToolSizes,
  getSharedLayerSize,
  getWorkspaceSize,
} from './platform-tool-sizes'
import { getSystemMetrics } from './systemMetrics'
import { AdaptiveZoomController } from './adaptiveZoom'

const sourceProduction = !app.isPackaged
const shouldManageBackend = getRuntimeEnv('GPTBRIDGE_MANAGE_BACKEND') === '1'

const MIN_UI_ZOOM = 0.85
const MAX_UI_ZOOM = 1.3

let currentUiZoom = 1
const adaptiveZoomController = new AdaptiveZoomController(() => currentUiZoom)

export function getCurrentUiZoom(): number {
  return currentUiZoom
}

export function setCurrentUiZoom(value: number): void {
  currentUiZoom = value
}

export function getAdaptiveZoomController(): AdaptiveZoomController {
  return adaptiveZoomController
}

function clampUiZoom(value: number): number {
  return Math.max(MIN_UI_ZOOM, Math.min(MAX_UI_ZOOM, value))
}

const isPathInside = (basePath: string, targetPath: string) => {
  const relative = path.relative(basePath, targetPath)
  return (
    relative === '' ||
    (!!relative && !relative.startsWith('..') && !path.isAbsolute(relative))
  )
}

export function registerIpcHandlers(
  mainWindowGetter: () => BrowserWindow | null
): void {
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
    const mainWindow = mainWindowGetter()
    if (!mainWindow || mainWindow.isDestroyed()) return { ok: false }
    mainWindow.reload()
    return { ok: true }
  })

  ipcMain.handle('app:reload-window-hard', async () => {
    const mainWindow = mainWindowGetter()
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
    const mainWindow = mainWindowGetter()
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
    const mainWindow = mainWindowGetter()
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
    const mainWindow = mainWindowGetter()
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
