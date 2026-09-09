import { useCallback, useEffect, useRef, useState, type CSSProperties } from 'react'

export interface ToolRunResult {
  ok?: boolean
  message?: string
  tool_id?: string
  request_id?: string
  stdout?: string
  stderr?: string
  stdout_encoding?: string
  stderr_encoding?: string
  exit_code?: number
  cancelled?: boolean
}

export interface OpenPathResult {
  ok?: boolean
  message?: string
  tool_id?: string
  request_id?: string
  cancelled?: boolean
}


export function waitForIpcEvent<T = Record<string, unknown>>(
  eventName: string,
  timeoutMs: number,
  predicate?: (payload: Record<string, unknown>) => boolean,
  signal?: AbortSignal
): Promise<T> {
  return new Promise((resolve, reject) => {
    let timer = 0
    let settled = false

    const cleanup = () => {
      window.clearTimeout(timer)
      window.removeEventListener('ipc_event', handler)
      signal?.removeEventListener('abort', handleAbort)
    }

    const rejectOnce = (error: Error) => {
      if (settled) return
      settled = true
      cleanup()
      reject(error)
    }

    const handler = (event: Event) => {
      const customEvent = event as CustomEvent
      const detail = customEvent.detail || {}
      if (detail.event !== eventName) return
      const payload = (detail.payload || {}) as Record<string, unknown>
      if (predicate && !predicate(payload)) return
      if (settled) return
      settled = true
      cleanup()
      resolve(payload as T)
    }

    const handleAbort = () => {
      const reason = signal?.reason
      rejectOnce(
        reason instanceof Error ? reason : new Error(`Waiting for ${eventName} was aborted.`)
      )
    }

    if (signal?.aborted) {
      handleAbort()
      return
    }

    timer = window.setTimeout(() => {
      rejectOnce(new Error(`Timed out waiting for ${eventName}.`))
    }, timeoutMs)
    window.addEventListener('ipc_event', handler)
    signal?.addEventListener('abort', handleAbort, { once: true })
  })
}

export function parseToolJson<T = Record<string, unknown>>(
  stdout: string | undefined
): T | null {
  const text = String(stdout || '').trim()
  if (!text) return null
  try {
    const parsed = JSON.parse(text)
    return parsed && typeof parsed === 'object' ? (parsed as T) : null
  } catch {
    return null
  }
}

export function formatRunOutput(result: ToolRunResult | null): string {
  if (!result) return ''
  const parts = [
    result.stdout ? `輸出\n${result.stdout.trim()}` : '',
    result.stderr ? `錯誤\n${result.stderr.trim()}` : '',
  ].filter(Boolean)
  if (parts.length > 0) return parts.join('\n\n')
  return result.message || '工具已完成，但沒有輸出。'
}

export function formatFileSize(size: number | null | undefined): string {
  if (typeof size !== 'number' || !Number.isFinite(size) || size < 0) return ''
  if (size < 1024) return `${size} B`
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`
  if (size < 1024 * 1024 * 1024) {
    return `${(size / 1024 / 1024).toFixed(1)} MB`
  }
  return `${(size / 1024 / 1024 / 1024).toFixed(1)} GB`
}

export function isAbsoluteFilesystemPath(value: string): boolean {
  return /^[a-zA-Z]:[\\/]/.test(value) || /^\\\\/.test(value) || value.startsWith('/')
}

export async function selectFolder(): Promise<string> {
  if (window.gptBridge?.selectFolder) return await window.gptBridge.selectFolder()
  return String((await (window as any).electron?.invoke?.('dialog:select-folder')) || '')
}

export async function openFile(): Promise<string> {
  if (window.gptBridge?.openFile) return await window.gptBridge.openFile()
  return String((await (window as any).electron?.invoke?.('dialog:open-file')) || '')
}

export async function openPath(payload: Record<string, unknown>): Promise<OpenPathResult> {
  if (window.gptBridge?.openPath) return await window.gptBridge.openPath(payload)
  return ((await (window as any).electron?.invoke?.('app:open-path', payload)) || {
    ok: false,
    message: '目前環境不支援開啟路徑。',
  }) as OpenPathResult
}

export type ToolRunOptions = {
  queueTtlMs?: number
  timeoutMs?: number
  onRequestId?: (requestId: string) => void
  signal?: AbortSignal
}
