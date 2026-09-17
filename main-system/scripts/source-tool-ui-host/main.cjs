const { app, BrowserView, BrowserWindow, dialog, ipcMain, Menu, shell } = require('electron')
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
  // A tool window never keeps a browser view over unrelated UI: detach on
  // minimize/hide, re-clamp on resize, close sessions with the window.
  mainWindow.on('minimize', detachAllBrowserSessions)
  mainWindow.on('hide', detachAllBrowserSessions)
  mainWindow.on('resize', () => {
    for (const session of browserSessions.values()) {
      if (!session.visible || !session.bounds) continue
      const clamped = clampBrowserBounds(session.bounds)
      if (!clamped) {
        detachBrowserSession(session)
        continue
      }
      session.bounds = clamped
      session.view.setBounds(clamped)
    }
  })
  mainWindow.on('closed', () => {
    for (const sessionId of Array.from(browserSessions.keys())) {
      closeBrowserSession(sessionId)
    }
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

// ---------------------------------------------------------------------------
// Embedded browser — hosted INSIDE this tool window.
//
// Each tool window owns its BrowserView so web content appears inside the
// external-collaboration UI.  The session store of the main-system process is
// never used from here: a tool window can no longer cover the main system.
// Invariants: sessions are created detached, bounds are clamped to this
// window's content area, showing requires valid bounds, and the view detaches
// when the window is minimized, hidden or closed.
// ---------------------------------------------------------------------------
const browserSessions = new Map()

function clampBrowserBounds(bounds) {
  if (!mainWindow || mainWindow.isDestroyed()) return null
  const content = mainWindow.getContentBounds()
  const raw = bounds && typeof bounds === 'object' ? bounds : {}
  const x = Math.max(0, Math.min(Math.round(Number(raw.x) || 0), content.width))
  const y = Math.max(0, Math.min(Math.round(Number(raw.y) || 0), content.height))
  const width = Math.max(0, Math.min(Math.round(Number(raw.width) || 0), content.width - x))
  const height = Math.max(0, Math.min(Math.round(Number(raw.height) || 0), content.height - y))
  if (width < 1 || height < 1) return null
  return { x, y, width, height }
}

function detachBrowserSession(session) {
  if (session.visible && mainWindow && !mainWindow.isDestroyed()) {
    try {
      mainWindow.removeBrowserView(session.view)
    } catch {
      // already detached
    }
  }
  session.visible = false
}

function createBrowserSession(id, ownerModule, url, bounds) {
  if (!mainWindow || mainWindow.isDestroyed()) {
    return { ok: false, message: 'MAIN_WINDOW_NOT_AVAILABLE' }
  }
  const existing = browserSessions.get(id)
  if (existing) {
    if (url) void existing.view.webContents.loadURL(url).catch(() => {})
    existing.url = url || existing.url
    if (bounds) existing.bounds = clampBrowserBounds(bounds)
    return { ok: true, id, url: existing.url }
  }
  const view = new BrowserView({
    webPreferences: { contextIsolation: true, nodeIntegration: false, sandbox: true },
  })
  if (url) void view.webContents.loadURL(url).catch(() => {})
  browserSessions.set(id, {
    id,
    view,
    ownerModule: String(ownerModule || ''),
    url: String(url || ''),
    bounds: bounds ? clampBrowserBounds(bounds) : null,
    visible: false,
  })
  return { ok: true, id, url: String(url || '') }
}

function findBrowserSession(id) {
  const session = browserSessions.get(String(id || ''))
  return session || null
}

function resizeBrowserSession(id, bounds) {
  const session = findBrowserSession(id)
  if (!session) return { ok: false, message: 'SESSION_NOT_FOUND' }
  const clamped = clampBrowserBounds(bounds)
  if (!clamped) {
    detachBrowserSession(session)
    session.bounds = null
    return { ok: true, hidden: true }
  }
  session.bounds = clamped
  if (session.visible) session.view.setBounds(clamped)
  return { ok: true, hidden: false }
}

function showBrowserSession(id) {
  const session = findBrowserSession(id)
  if (!session) return { ok: false, message: 'SESSION_NOT_FOUND' }
  if (!mainWindow || mainWindow.isDestroyed()) {
    return { ok: false, message: 'MAIN_WINDOW_NOT_AVAILABLE' }
  }
  const clamped = session.bounds ? clampBrowserBounds(session.bounds) : null
  if (!clamped) return { ok: false, message: 'BROWSER_VIEW_BOUNDS_REQUIRED' }
  session.bounds = clamped
  if (!session.visible) {
    mainWindow.addBrowserView(session.view)
    session.visible = true
  }
  session.view.setBounds(clamped)
  mainWindow.setTopBrowserView(session.view)
  session.view.webContents.focus()
  return { ok: true, bounds: clamped }
}

function hideBrowserSession(id) {
  const session = findBrowserSession(id)
  if (!session) return { ok: false, message: 'SESSION_NOT_FOUND' }
  detachBrowserSession(session)
  return { ok: true }
}

function closeBrowserSession(id) {
  const session = findBrowserSession(id)
  if (!session) return { ok: false, message: 'SESSION_NOT_FOUND' }
  detachBrowserSession(session)
  try {
    session.view.webContents.destroy()
  } catch {
    // already destroyed
  }
  browserSessions.delete(session.id)
  return { ok: true }
}

function closeModuleBrowserSessions(ownerModule) {
  let closed = 0
  for (const session of Array.from(browserSessions.values())) {
    if (session.ownerModule !== String(ownerModule || '')) continue
    detachBrowserSession(session)
    try {
      session.view.webContents.destroy()
    } catch {
      // already destroyed
    }
    browserSessions.delete(session.id)
    closed += 1
  }
  return closed
}

function detachAllBrowserSessions() {
  for (const session of browserSessions.values()) detachBrowserSession(session)
}

ipcMain.handle('embedded-browser:create', (_event, args = {}) =>
  createBrowserSession(args.id, args.ownerModule, args.url, args.bounds)
)
ipcMain.handle('embedded-browser:navigate', (_event, args = {}) => {
  const session = findBrowserSession(args.id)
  if (!session) return { ok: false, message: 'SESSION_NOT_FOUND' }
  session.url = String(args.url || '')
  void session.view.webContents.loadURL(session.url).catch(() => {})
  return { ok: true }
})
ipcMain.handle('embedded-browser:execute', async (_event, args = {}) => {
  const session = findBrowserSession(args.id)
  if (!session) return { ok: false, message: 'SESSION_NOT_FOUND' }
  try {
    const result = await session.view.webContents.executeJavaScript(String(args.script || ''))
    return { ok: true, result }
  } catch (error) {
    return { ok: false, message: String(error) }
  }
})
ipcMain.handle('embedded-browser:show', (_event, args = {}) => showBrowserSession(args.id))
ipcMain.handle('embedded-browser:hide', (_event, args = {}) => hideBrowserSession(args.id))
ipcMain.handle('embedded-browser:close', (_event, args = {}) => closeBrowserSession(args.id))
ipcMain.handle('embedded-browser:resize', (_event, args = {}) =>
  resizeBrowserSession(args.id, args.bounds)
)
ipcMain.handle('embedded-browser:list', () =>
  Array.from(browserSessions.values()).map((session) => ({
    id: session.id,
    ownerModule: session.ownerModule,
    url: session.url,
  }))
)
ipcMain.handle('embedded-browser:url', (_event, args = {}) => {
  const session = findBrowserSession(args.id)
  if (!session) return { ok: false, url: null }
  let url = session.url
  try {
    url = session.view.webContents.getURL() || session.url
  } catch {
    // keep the stored url
  }
  return { ok: true, url }
})
ipcMain.handle('embedded-browser:close-module', (_event, args = {}) => ({
  ok: true,
  closed: closeModuleBrowserSessions(args.ownerModule),
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
