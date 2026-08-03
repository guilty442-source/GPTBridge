const { app, BrowserWindow, dialog, ipcMain, Menu, shell } = require('electron')
const fs = require('node:fs')
const path = require('node:path')

const toolId = String(process.env.GPTBRIDGE_SOURCE_UI_TOOL_ID || '').trim()
const workspaceRoot = path.resolve(
  String(process.env.GPTBRIDGE_SOURCE_UI_WORKSPACE_ROOT || '')
)
const toolRoot = path.resolve(String(process.env.GPTBRIDGE_SOURCE_UI_TOOL_ROOT || ''))
const cacheRoot = path.resolve(
  String(process.env.GPTBRIDGE_TOOL_CACHE_ROOT || path.join(toolRoot, 'runtime', 'cache'))
)
const rendererEntry = path.resolve(
  String(process.env.GPTBRIDGE_SOURCE_UI_RENDERER_ENTRY || '')
)
const websocketUrl = String(process.env.GPTBRIDGE_SOURCE_UI_WEBSOCKET_URL || '').trim()
const toolVersion = String(process.env.GPTBRIDGE_SOURCE_UI_VERSION || '1.0.0').trim()
const openPathCapabilities = new Set()
const backendSessionUrl = (() => {
  try {
    return new URL(websocketUrl)
  } catch {
    return null
  }
})()

function isInside(base, target) {
  const relative = path.relative(base, target)
  return relative === '' || Boolean(relative && !relative.startsWith('..') && !path.isAbsolute(relative))
}

function validateConfiguration() {
  if (!/^[a-z0-9][a-z0-9_-]{1,63}$/.test(toolId)) return false
  if (!isInside(workspaceRoot, toolRoot)) return false
  if (!isInside(workspaceRoot, cacheRoot)) return false
  if (!isInside(workspaceRoot, rendererEntry) || !fs.existsSync(rendererEntry)) return false
  if (!backendSessionUrl) return false
  const port = Number(backendSessionUrl.port)
  const token = String(backendSessionUrl.searchParams.get('token') || '').trim()
  const instance = String(backendSessionUrl.searchParams.get('instance') || '').trim()
  return (
    backendSessionUrl.protocol === 'ws:' &&
    backendSessionUrl.hostname === '127.0.0.1' &&
    Number.isInteger(port) &&
    port >= 1024 &&
    port <= 65535 &&
    /^[a-f0-9]{64}$/i.test(token) &&
    /^[a-f0-9]{24}$/i.test(instance)
  )
}

if (!validateConfiguration()) {
  app.exit(1)
} else {
  const userDataRoot = path.join(cacheRoot, 'source-ui-user-data')
  const sessionDataRoot = path.join(cacheRoot, 'session-data')
  const diskCacheRoot = path.join(cacheRoot, 'disk-cache')
  fs.mkdirSync(userDataRoot, { recursive: true })
  fs.mkdirSync(sessionDataRoot, { recursive: true })
  fs.mkdirSync(diskCacheRoot, { recursive: true })
  app.setPath('userData', userDataRoot)
  app.setPath('sessionData', sessionDataRoot)
  app.commandLine.appendSwitch('disk-cache-dir', diskCacheRoot)
}

const hasSingleInstanceLock = app.requestSingleInstanceLock({ toolId })
if (!hasSingleInstanceLock) app.quit()

let mainWindow = null

function showWindow() {
  if (!mainWindow || mainWindow.isDestroyed()) return
  if (mainWindow.isMinimized()) mainWindow.restore()
  mainWindow.show()
  mainWindow.focus()
}

function createWindow() {
  if (mainWindow && !mainWindow.isDestroyed()) {
    showWindow()
    return
  }
  mainWindow = new BrowserWindow({
    width: Number(process.env.GPTBRIDGE_SOURCE_UI_WIDTH) || 1440,
    height: Number(process.env.GPTBRIDGE_SOURCE_UI_HEIGHT) || 920,
    minWidth: Number(process.env.GPTBRIDGE_SOURCE_UI_MIN_WIDTH) || 1120,
    minHeight: Number(process.env.GPTBRIDGE_SOURCE_UI_MIN_HEIGHT) || 760,
    show: false,
    backgroundColor: '#0b0f17',
    title: String(process.env.GPTBRIDGE_SOURCE_UI_TITLE || toolId),
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      backgroundThrottling: true,
      preload: path.join(__dirname, 'preload.cjs'),
    },
  })
  mainWindow.once('ready-to-show', showWindow)
  mainWindow.on('closed', () => {
    mainWindow = null
  })
  void mainWindow.loadFile(rendererEntry)
}

ipcMain.handle('app:ensure-backend-started', async () => ({
  ok: true,
  managed: true,
  runtimeMode: 'governed-source',
}))

ipcMain.handle('app:get-backend-session', async () => ({
  token: String(backendSessionUrl?.searchParams.get('token') || ''),
  websocketUrl,
  backendVersion: toolVersion,
  protocolVersion: 1,
  runtimeMode: 'governed-source',
}))

ipcMain.handle('dialog:select-folder', async () => {
  const result = await dialog.showOpenDialog({ properties: ['openDirectory'] })
  const selected = result.canceled ? '' : String(result.filePaths[0] || '')
  if (selected) openPathCapabilities.add(path.resolve(selected))
  return selected
})

ipcMain.handle('dialog:create-file', async (_event, defaultPath = '') => {
  const result = await dialog.showSaveDialog({ defaultPath: String(defaultPath || '') })
  const selected = result.canceled ? '' : String(result.filePath || '')
  if (selected) openPathCapabilities.add(path.resolve(selected))
  return selected
})

ipcMain.handle('dialog:open-file', async (_event, defaultPath = '') => {
  const result = await dialog.showOpenDialog({
    defaultPath: String(defaultPath || ''),
    properties: ['openFile'],
  })
  const selected = result.canceled ? '' : String(result.filePaths[0] || '')
  if (selected) openPathCapabilities.add(path.resolve(selected))
  return selected
})

ipcMain.handle('app:open-path', async (_event, payload = {}) => {
  const target = path.resolve(
    String(payload.path || '') ||
      path.resolve(String(payload.basePath || workspaceRoot), String(payload.relativePath || ''))
  )
  if (!isInside(workspaceRoot, target) && !openPathCapabilities.delete(target)) {
    return { ok: false, message: 'Path is outside the application workspace' }
  }
  if (String(payload.mode || 'open') === 'reveal') shell.showItemInFolder(target)
  else await shell.openPath(target)
  return { ok: true, path: target }
})

if (hasSingleInstanceLock) {
  app.on('second-instance', showWindow)
  app.whenReady().then(() => {
    Menu.setApplicationMenu(null)
    createWindow()
  })
}

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit()
})
