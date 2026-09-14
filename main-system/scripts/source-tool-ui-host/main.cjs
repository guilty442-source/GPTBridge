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
let reloadTimer = null
let hostRestartTimer = null

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

function startRendererWatch() {
  // Hot-reload: when the built renderer entry or this host's preload is
  // overwritten by a fresh build, reload the tool window without restarting
  // the whole app.  A host main-file change replaces the host process itself.
  const preloadEntry = path.join(__dirname, 'preload.cjs')
  const watchedEntries = [rendererEntry, preloadEntry].filter((entry) =>
    fs.existsSync(entry)
  )
  const scheduleReload = () => {
    if (!mainWindow || mainWindow.isDestroyed()) return
    if (reloadTimer) {
      clearTimeout(reloadTimer)
      reloadTimer = null
    }
    reloadTimer = setTimeout(() => {
      if (!mainWindow || mainWindow.isDestroyed()) return
      mainWindow.webContents.reloadIgnoringCache()
      console.log('[tool-ui-host] renderer.hot-reload', toolId)
    }, 500)
  }
  for (const entry of watchedEntries) {
    fs.watchFile(entry, { interval: 500 }, scheduleReload)
  }

  const hostEntry = path.join(__dirname, 'main.cjs')
  if (fs.existsSync(hostEntry)) {
    fs.watchFile(hostEntry, { interval: 500 }, () => {
      if (hostRestartTimer) {
        clearTimeout(hostRestartTimer)
        hostRestartTimer = null
      }
      hostRestartTimer = setTimeout(() => {
        hostRestartTimer = null
        try {
          const { spawn } = require('node:child_process')
          if (typeof app.releaseSingleInstanceLock === 'function') {
            app.releaseSingleInstanceLock()
          }
          const replacement = spawn(process.execPath, process.argv.slice(1), {
            cwd: process.cwd(),
            env: process.env,
            detached: true,
            stdio: 'ignore',
            windowsHide: true,
          })
          replacement.unref()
          console.log('[tool-ui-host] host.hot-restart', toolId)
        } catch (error) {
          console.log('[tool-ui-host] host.hot-restart-failed', String(error))
          return
        }
        app.exit(0)
      }, 800)
    })
  }
}

function stopRendererWatch() {
  if (reloadTimer) {
    clearTimeout(reloadTimer)
    reloadTimer = null
  }
  if (hostRestartTimer) {
    clearTimeout(hostRestartTimer)
    hostRestartTimer = null
  }
  fs.unwatchFile(rendererEntry)
  fs.unwatchFile(path.join(__dirname, 'preload.cjs'))
  fs.unwatchFile(path.join(__dirname, 'main.cjs'))
}

ipcMain.handle('app:ensure-backend-started', async () => ({
  ok: true,
  managed: true,
  runtimeMode: 'governed-source',
}))

// Embedded browser: the BrowserView session store lives in the main-system
// Electron process.  This host proxies the tool window's requests to that
// process over its token-guarded loopback bridge.
const EMBEDDED_BROWSER_CHANNELS = [
  'create',
  'navigate',
  'execute',
  'show',
  'hide',
  'close',
  'resize',
  'list',
  'url',
  'close-module',
]

function callEmbeddedBrowserBridge(channel, args) {
  return new Promise((resolve) => {
    let state = null
    try {
      const statePath = path.join(
        workspaceRoot,
        'main-system',
        'runtime',
        'state',
        'embedded-browser-bridge.json'
      )
      state = JSON.parse(fs.readFileSync(statePath, 'utf8'))
    } catch {
      resolve({ ok: false, message: 'EMBEDDED_BROWSER_BRIDGE_UNAVAILABLE' })
      return
    }
    const port = Number(state && state.port)
    const token = String((state && state.token) || '')
    const host = String((state && state.host) || '127.0.0.1')
    if (!Number.isInteger(port) || port <= 0 || port > 65535 || !token) {
      resolve({ ok: false, message: 'EMBEDDED_BROWSER_BRIDGE_INVALID' })
      return
    }
    const body = JSON.stringify({ channel, args })
    const request = require('node:http').request(
      {
        host,
        port,
        path: '/invoke',
        method: 'POST',
        timeout: 15_000,
        headers: {
          'Content-Type': 'application/json',
          'Content-Length': Buffer.byteLength(body),
          'X-GPTBridge-Bridge-Token': token,
        },
      },
      (response) => {
        const chunks = []
        response.on('data', (chunk) => chunks.push(chunk))
        response.on('end', () => {
          try {
            resolve(JSON.parse(Buffer.concat(chunks).toString('utf8')))
          } catch {
            resolve({ ok: false, message: 'EMBEDDED_BROWSER_BRIDGE_RESPONSE_INVALID' })
          }
        })
      }
    )
    request.on('timeout', () => request.destroy())
    request.on('error', () =>
      resolve({ ok: false, message: 'EMBEDDED_BROWSER_BRIDGE_UNAVAILABLE' })
    )
    request.write(body)
    request.end()
  })
}

for (const operation of EMBEDDED_BROWSER_CHANNELS) {
  ipcMain.handle(`embedded-browser:${operation}`, (_event, args = {}) =>
    callEmbeddedBrowserBridge(`embedded-browser:${operation}`, args || {})
  )
}

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

ipcMain.handle('dialog:validate-folder', async (_event, candidate = '') => {
  const selected = String(candidate || '').trim()
  if (!selected || !path.isAbsolute(selected)) return ''
  try {
    const resolved = fs.realpathSync.native(selected)
    if (!fs.statSync(resolved).isDirectory()) return ''
    openPathCapabilities.add(resolved)
    return resolved
  } catch {
    return ''
  }
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
    startRendererWatch()
  })
}

app.on('window-all-closed', () => {
  stopRendererWatch()
  if (process.platform !== 'darwin') app.quit()
})

app.on('before-quit', () => {
  stopRendererWatch()
})
