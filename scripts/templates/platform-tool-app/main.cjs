const { app, BrowserWindow, dialog, ipcMain, Menu, shell } = require('electron')
const childProcess = require('node:child_process')
const fs = require('node:fs')
const net = require('node:net')
const path = require('node:path')

const appRoot = __dirname
const manifestPath = path.join(appRoot, 'manifest.json')

function readJson(filePath) {
  try {
    return JSON.parse(fs.readFileSync(filePath, 'utf-8'))
  } catch {
    return {}
  }
}

const manifest = readJson(manifestPath)
const toolId = String(process.env.GPTBRIDGE_TOOL_ID || manifest.id || '').trim()
const toolName = String(manifest.name || toolId || 'GPTBridge Application')
let mainWindow = null
let backendProcess = null

app.setName(toolName)

function inferProjectRoot() {
  const explicit = String(process.env.GPTBRIDGE_PROJECT_ROOT || '').trim()
  if (explicit) return explicit

  const exeDir = path.dirname(process.execPath)
  const candidates = [
    path.resolve(exeDir, '..', '..', '..'),
    path.resolve(exeDir, '..', '..', '..', '..'),
    path.resolve(process.cwd(), '..', '..'),
    process.cwd(),
  ]

  for (const candidate of candidates) {
    if (fs.existsSync(path.join(candidate, 'src-core', 'main.py'))) {
      return candidate
    }
  }
  return candidates[0]
}

function isPathInside(basePath, targetPath) {
  const relative = path.relative(basePath, targetPath)
  return (
    relative === '' ||
    (relative && !relative.startsWith('..') && !path.isAbsolute(relative))
  )
}

function backendListening(timeoutMs = 750) {
  return new Promise((resolve) => {
    const socket = net.createConnection({ host: '127.0.0.1', port: 8765 })
    const finish = (value) => {
      socket.removeAllListeners()
      socket.destroy()
      resolve(value)
    }
    socket.setTimeout(timeoutMs)
    socket.once('connect', () => finish(true))
    socket.once('timeout', () => finish(false))
    socket.once('error', () => finish(false))
  })
}

function resolvePython(projectRoot) {
  const candidates = [
    path.join(projectRoot, '.venv', 'Scripts', 'python.exe'),
    path.join(projectRoot, '.venv', 'bin', 'python'),
    'python',
  ]
  return candidates.find((candidate) => candidate === 'python' || fs.existsSync(candidate)) || 'python'
}

async function ensureBackendStarted() {
  if (await backendListening()) {
    return { ok: true, alreadyRunning: true }
  }

  const projectRoot = inferProjectRoot()
  const backendEntry = path.join(projectRoot, 'src-core', 'main.py')
  if (!fs.existsSync(backendEntry)) {
    return {
      ok: false,
      message: `Backend entry not found: ${backendEntry}`,
    }
  }

  if (!backendProcess || backendProcess.exitCode !== null) {
    const python = resolvePython(projectRoot)
    backendProcess = childProcess.spawn(python, [backendEntry, '--serve'], {
      cwd: projectRoot,
      detached: true,
      stdio: 'ignore',
      windowsHide: true,
      env: {
        ...process.env,
        PYTHONUTF8: '1',
        PYTHONIOENCODING: 'utf-8',
        GPTBRIDGE_PROJECT_ROOT: projectRoot,
      },
    })
    backendProcess.unref()
  }

  const startedAt = Date.now()
  while (Date.now() - startedAt < 15000) {
    if (await backendListening(500)) {
      return { ok: true, started: true }
    }
    await new Promise((resolve) => setTimeout(resolve, 500))
  }

  return { ok: false, message: 'Backend startup timeout' }
}

function createWindow() {
  if (mainWindow && !mainWindow.isDestroyed()) {
    mainWindow.focus()
    return
  }

  const windowConfig =
    manifest.window && typeof manifest.window === 'object' ? manifest.window : {}

  mainWindow = new BrowserWindow({
    width: Number(windowConfig.width) || 1180,
    height: Number(windowConfig.height) || 820,
    minWidth: Number(windowConfig.minWidth) || 900,
    minHeight: Number(windowConfig.minHeight) || 620,
    show: false,
    backgroundColor: '#0b0f17',
    title: String(windowConfig.title || toolName),
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false,
      preload: path.join(appRoot, 'preload.cjs'),
    },
  })

  mainWindow.once('ready-to-show', () => {
    mainWindow.show()
    mainWindow.focus()
  })

  mainWindow.on('closed', () => {
    mainWindow = null
  })

  mainWindow.webContents.on('did-fail-load', (_event, code, desc) => {
    console.error('[GPTBridge Tool] Renderer load failed:', code, desc)
  })

  mainWindow.webContents.on('render-process-gone', (_event, details) => {
    console.error('[GPTBridge Tool] Renderer process gone:', details)
  })

  if (process.env.GPTBRIDGE_OPEN_DEVTOOLS === '1') {
    mainWindow.webContents.openDevTools({ mode: 'detach' })
  }

  mainWindow.loadFile(path.join(appRoot, 'renderer', 'index.html'))
}

ipcMain.handle('app:ensure-backend-started', async () => ensureBackendStarted())

ipcMain.handle('dialog:select-folder', async () => {
  const result = await dialog.showOpenDialog({ properties: ['openDirectory'] })
  return result.canceled ? '' : result.filePaths[0] || ''
})

ipcMain.handle('dialog:create-file', async (_event, defaultPath = '') => {
  const result = await dialog.showSaveDialog({
    defaultPath: String(defaultPath || ''),
  })
  return result.canceled ? '' : result.filePath || ''
})

ipcMain.handle('dialog:open-file', async (_event, defaultPath = '') => {
  const result = await dialog.showOpenDialog({
    defaultPath: String(defaultPath || ''),
    properties: ['openFile'],
  })
  return result.canceled ? '' : result.filePaths[0] || ''
})

ipcMain.handle('app:open-path', async (_event, payload = {}) => {
  const projectRoot = inferProjectRoot()
  const rawPath = String(payload.path || '').trim()
  const basePath = String(payload.basePath || projectRoot).trim()
  const relativePath = String(payload.relativePath || '').trim()
  const mode = String(payload.mode || 'open')
  const targetPath = rawPath
    ? path.resolve(rawPath)
    : path.resolve(basePath, relativePath)

  if (!isPathInside(projectRoot, targetPath) && !path.isAbsolute(rawPath)) {
    return { ok: false, message: 'Path is outside the application workspace' }
  }

  try {
    if (mode === 'reveal') {
      shell.showItemInFolder(targetPath)
    } else {
      await shell.openPath(targetPath)
    }
    return { ok: true, path: targetPath }
  } catch (error) {
    return {
      ok: false,
      message: error instanceof Error ? error.message : String(error),
    }
  }
})

app.whenReady().then(() => {
  Menu.setApplicationMenu(null)
  createWindow()
  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow()
  })
})

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit()
})
