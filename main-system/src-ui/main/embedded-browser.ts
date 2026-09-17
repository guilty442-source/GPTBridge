/**
 * embedded-browser.ts — unified in-app browser module using Electron BrowserView.
 *
 * Replaces external Playwright/Chrome/Edge browser automation with a single
 * embedded BrowserView that lives inside the main Electron window.
 *
 * All tools (ai-collaboration, vaultly) use this module instead of launching
 * external browser processes.  No Playwright, no Chrome, no Edge dependency.
 *
 * Governance: A44/E30 (four-functions-local) + A49/E35 (formal-tools-local).
 */

import { app, BrowserView, BrowserWindow, ipcMain } from 'electron'
import path from 'node:path'

interface BrowserBounds {
  x: number
  y: number
  width: number
  height: number
}

interface EmbeddedBrowserSession {
  id: string
  view: BrowserView
  ownerModule: string
  url: string
  createdAt: number
  bounds: BrowserBounds | null
  visible: boolean
}

const sessions = new Map<string, EmbeddedBrowserSession>()
let mainWindowRef: BrowserWindow | null = null

/**
 * Clamp bounds to the window content area.
 *
 * Returns null for zero/negative-size bounds so a hidden or collapsed panel
 * can never produce a full-window overlay (the screen-pollution defect).
 */
function clampBounds(bounds: BrowserBounds): BrowserBounds | null {
  if (!mainWindowRef || mainWindowRef.isDestroyed()) return null
  const content = mainWindowRef.getContentBounds()
  const x = Math.max(0, Math.min(Math.round(bounds.x), content.width))
  const y = Math.max(0, Math.min(Math.round(bounds.y), content.height))
  const width = Math.max(0, Math.min(Math.round(bounds.width), content.width - x))
  const height = Math.max(0, Math.min(Math.round(bounds.height), content.height - y))
  if (width < 1 || height < 1) return null
  return { x, y, width, height }
}

function detachView(session: EmbeddedBrowserSession): void {
  if (
    session.visible &&
    mainWindowRef &&
    !mainWindowRef.isDestroyed()
  ) {
    mainWindowRef.removeBrowserView(session.view)
  }
  session.visible = false
}

/**
 * Register the main window reference so BrowserViews can be attached.
 * Called once during app initialization.
 *
 * Lifecycle guards: views detach when the window is minimized, hidden or
 * closed, and visible bounds are re-clamped on resize, so an embedded
 * browser can never linger over unrelated UI.
 */
export function registerEmbeddedBrowser(mainWindow: BrowserWindow): void {
  mainWindowRef = mainWindow
  const hideAll = (): void => {
    for (const session of sessions.values()) detachView(session)
  }
  mainWindow.on('minimize', hideAll)
  mainWindow.on('hide', hideAll)
  mainWindow.on('closed', () => {
    closeAllSessions()
    mainWindowRef = null
  })
  mainWindow.on('resize', () => {
    for (const session of sessions.values()) {
      if (!session.visible || !session.bounds) continue
      const clamped = clampBounds(session.bounds)
      if (!clamped) {
        detachView(session)
        continue
      }
      session.bounds = clamped
      session.view.setBounds(clamped)
    }
  })
}

/**
 * Create or reuse an embedded browser session for a given module + key.
 *
 * The session starts DETACHED from the window: optional bounds are stored
 * (clamped) but nothing is displayed until an explicit ``showSession``.
 * No external browser process is launched.
 */
export function createSession(
  id: string,
  ownerModule: string,
  url: string,
  bounds?: BrowserBounds,
): { ok: boolean; id: string; url: string; message?: string } {
  if (!mainWindowRef || mainWindowRef.isDestroyed()) {
    return { ok: false, id, url, message: 'MAIN_WINDOW_NOT_AVAILABLE' }
  }

  // Sessions are created DETACHED: nothing appears on screen until an
  // explicit showSession with valid, clamped bounds.  There is no
  // full-window default, so creating a session can never pollute the UI.
  const existing = sessions.get(id)
  if (existing) {
    existing.view.webContents.loadURL(url).catch(() => {
      // navigation errors are non-fatal
    })
    existing.url = url
    if (bounds) existing.bounds = clampBounds(bounds)
    return { ok: true, id, url }
  }

  const view = new BrowserView({
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  })

  view.webContents.loadURL(url).catch(() => {
    // navigation errors are non-fatal
  })

  sessions.set(id, {
    id,
    view,
    ownerModule,
    url,
    createdAt: Date.now(),
    bounds: bounds ? clampBounds(bounds) : null,
    visible: false,
  })

  return { ok: true, id, url }
}

/**
 * Navigate an existing session to a new URL.
 */
export function navigateSession(
  id: string,
  url: string,
): { ok: boolean; message?: string } {
  const session = sessions.get(id)
  if (!session) {
    return { ok: false, message: 'SESSION_NOT_FOUND' }
  }
  session.url = url
  session.view.webContents.loadURL(url).catch(() => {
    // non-fatal
  })
  return { ok: true }
}

/**
 * Get the current URL of a session.
 */
export function getSessionUrl(id: string): string | null {
  const session = sessions.get(id)
  if (!session) {
    return null
  }
  return session.view.webContents.getURL() || session.url
}

/**
 * Execute JavaScript in a session's webContents and return the result.
 * Used for form filling, response extraction, etc.
 */
export async function executeScript<T = unknown>(
  id: string,
  script: string,
): Promise<{ ok: boolean; result?: T; message?: string }> {
  const session = sessions.get(id)
  if (!session) {
    return { ok: false, message: 'SESSION_NOT_FOUND' }
  }
  try {
    const result = await session.view.webContents.executeJavaScript(script)
    return { ok: true, result: result as T }
  } catch (error) {
    return { ok: false, message: String(error) }
  }
}

/**
 * Resize a session's BrowserView bounds.
 */
export function resizeSession(
  id: string,
  bounds: BrowserBounds,
): { ok: boolean; message?: string; hidden?: boolean } {
  const session = sessions.get(id)
  if (!session) {
    return { ok: false, message: 'SESSION_NOT_FOUND' }
  }
  const clamped = clampBounds(bounds)
  if (!clamped) {
    detachView(session)
    session.bounds = null
    return { ok: true, hidden: true }
  }
  session.bounds = clamped
  if (session.visible) {
    session.view.setBounds(clamped)
  }
  return { ok: true, hidden: false }
}

/**
 * Show a session inside its clamped bounds (bring to front).
 *
 * A session without valid bounds stays hidden: showing it would otherwise
 * create the full-window overlay that polluted the screen.
 */
export function showSession(id: string): {
  ok: boolean
  message?: string
  bounds?: BrowserBounds
} {
  const session = sessions.get(id)
  if (!session) {
    return { ok: false, message: 'SESSION_NOT_FOUND' }
  }
  if (!mainWindowRef || mainWindowRef.isDestroyed()) {
    return { ok: false, message: 'MAIN_WINDOW_NOT_AVAILABLE' }
  }
  const clamped = session.bounds ? clampBounds(session.bounds) : null
  if (!clamped) {
    return { ok: false, message: 'BROWSER_VIEW_BOUNDS_REQUIRED' }
  }
  session.bounds = clamped
  if (!session.visible) {
    mainWindowRef.addBrowserView(session.view)
    session.visible = true
  }
  session.view.setBounds(clamped)
  mainWindowRef.setTopBrowserView(session.view)
  session.view.webContents.focus()
  return { ok: true, bounds: clamped }
}

/**
 * Hide a session (remove from view but keep alive).
 */
export function hideSession(id: string): { ok: boolean; message?: string } {
  const session = sessions.get(id)
  if (!session) {
    return { ok: false, message: 'SESSION_NOT_FOUND' }
  }
  detachView(session)
  return { ok: true }
}

/**
 * Close and destroy a session.
 */
export function closeSession(id: string): { ok: boolean; message?: string } {
  const session = sessions.get(id)
  if (!session) {
    return { ok: false, message: 'SESSION_NOT_FOUND' }
  }
  detachView(session)
  ;(session.view.webContents as unknown as { destroy?: () => void }).destroy?.()
  sessions.delete(id)
  return { ok: true }
}

/**
 * List all active sessions.
 */
export function listSessions(): Array<{
  id: string
  ownerModule: string
  url: string
  createdAt: number
}> {
  return Array.from(sessions.values()).map((s) => ({
    id: s.id,
    ownerModule: s.ownerModule,
    url: s.url,
    createdAt: s.createdAt,
  }))
}

/**
 * Close all sessions for a specific module.
 */
export function closeModuleSessions(ownerModule: string): number {
  let count = 0
  for (const [id, session] of sessions) {
    if (session.ownerModule === ownerModule) {
      closeSession(id)
      count++
    }
  }
  return count
}

/**
 * Close all sessions and clean up.
 */
export function closeAllSessions(): void {
  for (const id of sessions.keys()) {
    closeSession(id)
  }
}

/**
 * Register IPC handlers for the embedded browser.
 * Renderer process can call these to control browser sessions.
 */
export function registerEmbeddedBrowserIpc(): void {
  ipcMain.handle('embedded-browser:create', (_event, args) => {
    return createSession(args.id, args.ownerModule, args.url, args.bounds)
  })
  ipcMain.handle('embedded-browser:navigate', (_event, args) => {
    return navigateSession(args.id, args.url)
  })
  ipcMain.handle('embedded-browser:execute', async (_event, args) => {
    return executeScript(args.id, args.script)
  })
  ipcMain.handle('embedded-browser:show', (_event, args) => {
    return showSession(args.id)
  })
  ipcMain.handle('embedded-browser:hide', (_event, args) => {
    return hideSession(args.id)
  })
  ipcMain.handle('embedded-browser:close', (_event, args) => {
    return closeSession(args.id)
  })
  ipcMain.handle('embedded-browser:resize', (_event, args) => {
    return resizeSession(args.id, args.bounds)
  })
  ipcMain.handle('embedded-browser:list', () => {
    return listSessions()
  })
  ipcMain.handle('embedded-browser:url', (_event, args) => {
    const url = getSessionUrl(args.id)
    return { ok: url !== null, url }
  })
  ipcMain.handle('embedded-browser:close-module', (_event, args) => {
    return { closed: closeModuleSessions(args.ownerModule) }
  })
}
