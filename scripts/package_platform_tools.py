from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
PLATFORM_TOOLS_DIR = PROJECT_ROOT / "platform_tools"
ELECTRON_DIST_DIR = PROJECT_ROOT / "node_modules" / "electron" / "dist"
PLATFORM_RENDERER_DIR = PROJECT_ROOT / "dist-ui" / "platform-tools" / "renderer"

WRAPPER_MAIN = r"""
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
    backendProcess = childProcess.spawn(
      python,
      [backendEntry, '--serve'],
      {
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
      }
    )
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

  const windowConfig = manifest.window && typeof manifest.window === 'object'
    ? manifest.window
    : {}

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

  mainWindow.loadFile(path.join(appRoot, 'renderer', 'index.html'), {
    query: { tool: toolId },
  })
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
"""

WRAPPER_PRELOAD = r"""
const { contextBridge, ipcRenderer } = require('electron')

const allowedInvokeChannels = new Set([
  'app:ensure-backend-started',
  'app:open-path',
  'dialog:select-folder',
  'dialog:create-file',
  'dialog:open-file',
])

function invoke(channel, ...args) {
  if (!allowedInvokeChannels.has(channel)) {
    return Promise.reject(new Error(`Blocked IPC channel: ${channel}`))
  }
  return ipcRenderer.invoke(channel, ...args)
}

contextBridge.exposeInMainWorld('electron', { invoke })

contextBridge.exposeInMainWorld('gptBridge', {
  standaloneTool: true,
  selectFolder: () => invoke('dialog:select-folder'),
  createFile: (defaultPath = '') => invoke('dialog:create-file', defaultPath),
  openFile: (defaultPath = '') => invoke('dialog:open-file', defaultPath),
  openPath: (payload) => invoke('app:open-path', payload),
})
"""


def load_manifest(tool_dir: Path) -> dict[str, Any] | None:
    manifest_path = tool_dir / "manifest.json"
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def resolve_entry(tool_dir: Path, manifest: dict[str, Any]) -> Path:
    runtime = manifest.get("runtime")
    if isinstance(runtime, dict):
        runtime_entry = str(runtime.get("entry", "")).strip()
        if runtime_entry:
            return (tool_dir / runtime_entry).resolve()

    raw_entry = str(manifest.get("entry", "")).strip()
    if not raw_entry:
        return (tool_dir / "src" / "main.py").resolve()

    entry_path = PROJECT_ROOT / raw_entry
    if entry_path.suffix == "":
        entry_path = entry_path.with_suffix(".py")
    return entry_path.resolve()


def resolve_executable_name(tool_id: str, manifest: dict[str, Any]) -> str:
    executable = manifest.get("executable")
    if isinstance(executable, dict):
        raw_name = str(executable.get("name", "")).strip()
        if raw_name:
            return Path(raw_name).stem
        raw_path = str(executable.get("path", "")).strip()
        if raw_path:
            return Path(raw_path).stem
    return tool_id


def iter_tools(selected_ids: set[str] | None) -> list[tuple[str, Path, dict[str, Any]]]:
    tools: list[tuple[str, Path, dict[str, Any]]] = []
    if not PLATFORM_TOOLS_DIR.exists():
        return tools

    for tool_dir in sorted(PLATFORM_TOOLS_DIR.iterdir(), key=lambda item: item.name.lower()):
        if not tool_dir.is_dir() or tool_dir.name.startswith("_"):
            continue
        manifest = load_manifest(tool_dir)
        if not manifest:
            continue
        tool_id = str(manifest.get("id", tool_dir.name)).strip() or tool_dir.name
        if selected_ids is not None and tool_id not in selected_ids:
            continue
        tools.append((tool_id, tool_dir, manifest))
    return tools


def package_tool(tool_id: str, tool_dir: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    entry = resolve_entry(tool_dir, manifest)
    if entry.suffix.lower() != ".py":
        return {
            "ok": False,
            "tool_id": tool_id,
            "message": f"Python runtime entry must be a .py file: {entry}",
        }

    electron_exe = ELECTRON_DIST_DIR / "electron.exe"
    if not electron_exe.exists():
        return {
            "ok": False,
            "tool_id": tool_id,
            "message": f"Electron runtime not found: {electron_exe}",
        }

    renderer_index = PLATFORM_RENDERER_DIR / "index.html"
    if not renderer_index.exists():
        return {
            "ok": False,
            "tool_id": tool_id,
            "message": (
                "Platform renderer is missing. Run "
                "npm run build:platform-renderer before packaging tools."
            ),
        }

    executable_name = resolve_executable_name(tool_id, manifest)
    dist_dir = tool_dir / "dist"
    exe_path = dist_dir / f"{executable_name}.exe"

    try:
        if dist_dir.exists():
            shutil.rmtree(dist_dir)
        shutil.copytree(ELECTRON_DIST_DIR, dist_dir)

        copied_electron_exe = dist_dir / "electron.exe"
        if exe_path.exists():
            exe_path.unlink()
        copied_electron_exe.rename(exe_path)

        app_dir = dist_dir / "resources" / "app"
        if app_dir.exists():
            shutil.rmtree(app_dir)
        app_dir.mkdir(parents=True, exist_ok=True)
        shutil.copytree(PLATFORM_RENDERER_DIR, app_dir / "renderer")

        app_manifest = dict(manifest)
        app_manifest["id"] = tool_id
        (app_dir / "manifest.json").write_text(
            json.dumps(app_manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        (app_dir / "package.json").write_text(
            json.dumps(
                {
                    "name": f"gptbridge-tool-{tool_id}",
                    "version": str(manifest.get("version", "1.0.0")),
                    "main": "main.cjs",
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        (app_dir / "main.cjs").write_text(WRAPPER_MAIN.strip() + "\n", encoding="utf-8")
        (app_dir / "preload.cjs").write_text(
            WRAPPER_PRELOAD.strip() + "\n",
            encoding="utf-8",
        )
    except Exception as exc:
        return {
            "ok": False,
            "tool_id": tool_id,
            "entry": str(entry),
            "exe_path": str(exe_path),
            "message": str(exc),
        }

    return {
        "ok": exe_path.exists(),
        "tool_id": tool_id,
        "entry": str(entry),
        "exe_path": str(exe_path),
        "renderer_path": str(app_dir / "renderer"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Package platform tools as standalone EXEs.")
    parser.add_argument("tool_ids", nargs="*", help="Optional tool ids to package.")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args()

    selected_ids = set(args.tool_ids) if args.tool_ids else None
    results = [
        package_tool(tool_id, tool_dir, manifest)
        for tool_id, tool_dir, manifest in iter_tools(selected_ids)
    ]
    ok = all(result.get("ok") for result in results)

    if args.as_json:
        print(json.dumps({"ok": ok, "results": results}, ensure_ascii=False))
    else:
        for result in results:
            status = "OK" if result.get("ok") else "FAIL"
            print(f"[{status}] {result.get('tool_id')}: {result.get('exe_path') or result.get('message')}")

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
