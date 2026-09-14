/**
 * embedded-browser-bridge —loopback IPC bridge for the embedded browser.
 *
 * The embedded BrowserView lives in the main Electron process.  Tool UIs and
 * tool Python backends are separate processes, so they reach the same session
 * store through a token-guarded loopback HTTP endpoint owned by this process.
 * The endpoint never leaves 127.0.0.1 and requires the per-launch token that
 * is published to the runtime state file.
 */
import crypto from 'node:crypto'
import fs from 'node:fs'
import http from 'node:http'
import path from 'node:path'
import type { AddressInfo } from 'node:net'

import {
  closeModuleSessions,
  closeSession,
  createSession,
  executeScript,
  getSessionUrl,
  hideSession,
  listSessions,
  navigateSession,
  resizeSession,
  showSession,
} from './embedded-browser'
import { getRuntimePathLibrary } from './pathLibrary'

const BRIDGE_HOST = '127.0.0.1'
const TOKEN_HEADER = 'x-gptbridge-bridge-token'
const MAX_BODY_BYTES = 1_048_576

type BridgeHandler = (args: Record<string, unknown>) => unknown

const HANDLERS: Record<string, BridgeHandler> = {
  'embedded-browser:create': (args) =>
    createSession(
      String(args.id ?? ''),
      String(args.ownerModule ?? ''),
      String(args.url ?? ''),
      (args.bounds as { x: number; y: number; width: number; height: number } | undefined) ??
        undefined
    ),
  'embedded-browser:navigate': (args) =>
    navigateSession(String(args.id ?? ''), String(args.url ?? '')),
  'embedded-browser:execute': (args) =>
    executeScript(String(args.id ?? ''), String(args.script ?? '')),
  'embedded-browser:show': (args) => showSession(String(args.id ?? '')),
  'embedded-browser:hide': (args) => hideSession(String(args.id ?? '')),
  'embedded-browser:close': (args) => closeSession(String(args.id ?? '')),
  'embedded-browser:resize': (args) => {
    const bounds = (args.bounds ?? {}) as Record<string, unknown>
    return resizeSession(String(args.id ?? ''), {
      x: Number(bounds.x ?? 0),
      y: Number(bounds.y ?? 0),
      width: Number(bounds.width ?? 0),
      height: Number(bounds.height ?? 0),
    })
  },
  'embedded-browser:list': () => listSessions(),
  'embedded-browser:url': (args) => {
    const url = getSessionUrl(String(args.id ?? ''))
    return { ok: url !== null, url }
  },
  'embedded-browser:close-module': (args) => ({
    ok: true,
    closed: closeModuleSessions(String(args.ownerModule ?? '')),
  }),
}

let server: http.Server | null = null
let bridgeToken = ''
let bridgePort = 0

function statePath(): string {
  return path.join(
    getRuntimePathLibrary().workspaceRoot,
    'main-system',
    'runtime',
    'state',
    'embedded-browser-bridge.json'
  )
}

function publishState(): void {
  try {
    const target = statePath()
    fs.mkdirSync(path.dirname(target), { recursive: true })
    fs.writeFileSync(
      target,
      JSON.stringify(
        {
          host: BRIDGE_HOST,
          port: bridgePort,
          token: bridgeToken,
          pid: process.pid,
          started_at: new Date().toISOString(),
        },
        null,
        2
      ),
      'utf8'
    )
  } catch {
    // The bridge stays usable for same-process callers if publication fails.
  }
}

function removeState(): void {
  try {
    fs.rmSync(statePath(), { force: true })
  } catch {
    // best effort
  }
}

function readBody(request: http.IncomingMessage): Promise<string> {
  return new Promise((resolve, reject) => {
    const chunks: Buffer[] = []
    let size = 0
    request.on('data', (chunk: Buffer) => {
      size += chunk.length
      if (size > MAX_BODY_BYTES) {
        reject(new Error('BRIDGE_BODY_TOO_LARGE'))
        request.destroy()
        return
      }
      chunks.push(chunk)
    })
    request.on('end', () => resolve(Buffer.concat(chunks).toString('utf8')))
    request.on('error', reject)
  })
}

function respond(
  response: http.ServerResponse,
  status: number,
  payload: unknown
): void {
  const body = JSON.stringify(payload ?? {})
  response.writeHead(status, {
    'Content-Type': 'application/json; charset=utf-8',
    'Content-Length': Buffer.byteLength(body),
  })
  response.end(body)
}

async function handleRequest(
  request: http.IncomingMessage,
  response: http.ServerResponse
): Promise<void> {
  if (request.method !== 'POST' || request.url !== '/invoke') {
    respond(response, 404, { ok: false, message: 'NOT_FOUND' })
    return
  }
  const provided = String(request.headers[TOKEN_HEADER] ?? '')
  const expected = bridgeToken
  const authorized =
    provided.length > 0 &&
    provided.length === expected.length &&
    crypto.timingSafeEqual(Buffer.from(provided), Buffer.from(expected))
  if (!authorized) {
    respond(response, 403, { ok: false, message: 'BRIDGE_TOKEN_INVALID' })
    return
  }
  let payload: { channel?: unknown; args?: unknown }
  try {
    payload = JSON.parse(await readBody(request)) as {
      channel?: unknown
      args?: unknown
    }
  } catch {
    respond(response, 400, { ok: false, message: 'BRIDGE_BODY_INVALID' })
    return
  }
  const channel = String(payload.channel ?? '')
  const handler = HANDLERS[channel]
  if (!handler) {
    respond(response, 404, { ok: false, message: 'BRIDGE_CHANNEL_UNKNOWN' })
    return
  }
  const args =
    payload.args && typeof payload.args === 'object'
      ? (payload.args as Record<string, unknown>)
      : {}
  try {
    const result = await handler(args)
    respond(response, 200, result)
  } catch (error) {
    respond(response, 200, {
      ok: false,
      message: error instanceof Error ? error.message : String(error),
    })
  }
}

export function startEmbeddedBrowserBridge(): {
  ok: boolean
  port: number
} {
  if (server) return { ok: true, port: bridgePort }
  bridgeToken = crypto.randomBytes(32).toString('hex')
  server = http.createServer((request, response) => {
    void handleRequest(request, response)
  })
  server.on('error', () => {
    server = null
    bridgePort = 0
  })
  server.listen(0, BRIDGE_HOST, () => {
    const address = server?.address() as AddressInfo | null
    bridgePort = address?.port ?? 0
    publishState()
  })
  return { ok: true, port: bridgePort }
}

export function stopEmbeddedBrowserBridge(): void {
  removeState()
  const current = server
  server = null
  bridgePort = 0
  bridgeToken = ''
  if (current) {
    try {
      current.close()
    } catch {
      // best effort
    }
  }
}
