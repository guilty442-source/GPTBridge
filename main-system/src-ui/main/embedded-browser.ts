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

interface EmbeddedBrowserSession {
  id: string
  view: BrowserView
  ownerModule: string
  url: string
  createdAt: number
}

const sessions = new Map<string, EmbeddedBrowserSession>()
let mainWindowRef: BrowserWindow | null = null

/**
 * Register the main window reference so BrowserViews can be attached.
 * Called once during app initialization.
 */
export function registerEmbeddedBrowser(mainWindow: BrowserWindow): void {
  mainWindowRef = mainWindow
}

/**
 * Create or reuse an embedded browser session for a given module + key.
 *
 * The BrowserView is attached to the main window and sized to the content area.
 * No external browser process is launched.
 */
export function createSession(
  id: string,
  ownerModule: string,
  url: string,
  bounds?: { x: number; y: number; width: number; height: number },
): { ok: boolean; id: string; url: string; message?: string } {
  if (!mainWindowRef || mainWindowRef.isDestroyed()) {
    return { ok: false, id, url, message: 'MAIN_WINDOW_NOT_AVAILABLE' }
  }

  // Reuse existing session
  const existing = sessions.get(id)
  if (existing) {
    existing.view.webContents.loadURL(url).catch(() => {
      // navigation errors are non-fatal
    })
    existing.url = url
    return { ok: true, id, url }
  }

  const view = new BrowserView({
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  })

  mainWindowRef.addBrowserView(view)

  const contentBounds = mainWindowRef.getContentBounds()
  const viewBounds = bounds ?? {
    x: 0,
    y: 35,
    width: contentBounds.width,
    height: contentBounds.height - 35,
  }
  view.setBounds(viewBounds)

  view.webContents.loadURL(url).catch(() => {
    // navigation errors are non-fatal
  })

  sessions.set(id, {
    id,
    view,
    ownerModule,
    url,
    createdAt: Date.now(),
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
  bounds: { x: number; y: number; width: number; height: number },
): { ok: boolean; message?: string } {
  const session = sessions.get(id)
  if (!session) {
    return { ok: false, message: 'SESSION_NOT_FOUND' }
  }
  session.view.setBounds(bounds)
  return { ok: true }
}

/**
 * Show a session (bring to front).
 */
export function showSession(id: string): { ok: boolean; message?: string } {
  const session = sessions.get(id)
  if (!session) {
    return { ok: false, message: 'SESSION_NOT_FOUND' }
  }
  if (mainWindowRef && !mainWindowRef.isDestroyed()) {
    mainWindowRef.setTopBrowserView(session.view)
    session.view.webContents.focus()
  }
  return { ok: true }
}

/**
 * Hide a session (remove from view but keep alive).
 */
export function hideSession(id: string): { ok: boolean; message?: string } {
  const session = sessions.get(id)
  if (!session) {
    return { ok: false, message: 'SESSION_NOT_FOUND' }
  }
  if (mainWindowRef && !mainWindowRef.isDestroyed()) {
    mainWindowRef.removeBrowserView(session.view)
  }
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
  if (mainWindowRef && !mainWindowRef.isDestroyed()) {
    mainWindowRef.removeBrowserView(session.view)
  }
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
