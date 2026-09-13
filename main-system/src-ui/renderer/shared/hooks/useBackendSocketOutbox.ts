/**
 * Outbox event processing helpers for useBackendSocket.
 *
 * Extracts the A195 transactional outbox state-event handling
 * (session reset, sequence validation, dedup, gap detection,
 * cursor acknowledgement) out of the socket message handler.
 */

import type { OutboxStateEvent } from './useBackendSocketTypes'
import {
  OUTBOX_CURSOR_KEY,
  OUTBOX_GENERATION_KEY,
  OUTBOX_BUFFER_MAX,
} from './useBackendSocketTypes'
import { eventBus } from '../RuntimeEventBus'

export type OutboxContext = {
  outboxAppliedRef: { current: number }
  outboxBufferRef: { current: Map<number, OutboxStateEvent> }
  sessionGenerationRef: { current: string | null }
}

export type OutboxSendFn = (command: string, payload: unknown) => void

/**
 * Handle a `state_event_session` payload: detect backend-generation
 * changes, reset the cursor to the backend's authoritative value,
 * and invalidate the projection when the generation changes.
 */
export function handleOutboxSession(
  payload: unknown,
  ctx: OutboxContext
): void {
  const sess = payload as
    | {
        backend_generation?: string
        cursor?: number
        reset?: boolean
      }
    | undefined
  const storedGeneration = window.sessionStorage.getItem(OUTBOX_GENERATION_KEY)
  if (sess?.backend_generation) {
    ctx.sessionGenerationRef.current = sess.backend_generation
    const resetCursor =
      typeof sess.cursor === 'number' && Number.isFinite(sess.cursor)
        ? Math.max(0, sess.cursor)
        : null
    const generationChanged = storedGeneration !== sess.backend_generation
    if (sess.reset === true || generationChanged) {
      if (resetCursor !== null) {
        ctx.outboxAppliedRef.current = resetCursor
        window.sessionStorage.setItem(OUTBOX_CURSOR_KEY, String(resetCursor))
      }
      ctx.outboxBufferRef.current.clear()
      if (generationChanged) {
        eventBus.emit('state_event_invalidate', {
          reason: 'backend-generation-change',
        })
      }
    }
    window.sessionStorage.setItem(OUTBOX_GENERATION_KEY, sess.backend_generation)
  }
}

/**
 * Handle a `state_event` payload: validate sequence, dedup, apply
 * contiguous events, acknowledge the cursor, and detect gaps.
 *
 * Returns true if the event was consumed (applied or deduped),
 * false if it was a stale-backlog event that should be ignored.
 */
export function handleOutboxStateEvent(
  payload: unknown,
  ctx: OutboxContext,
  send: OutboxSendFn,
  requestRuntimeStatusThrottled: () => void
): boolean {
  const ev = payload as OutboxStateEvent | undefined
  const sessionGeneration = ctx.sessionGenerationRef.current
  const staleBacklog =
    ev !== undefined &&
    typeof ev.sequence === 'number' &&
    sessionGeneration !== null &&
    ev.backend_generation !== sessionGeneration
  if (staleBacklog) {
    return true // consumed (ignored)
  }
  if (!ev || typeof ev.sequence !== 'number') {
    return false
  }
  const storedGeneration = window.sessionStorage.getItem(OUTBOX_GENERATION_KEY)
  if (storedGeneration !== null && ev.backend_generation !== storedGeneration) {
    ctx.outboxAppliedRef.current = 0
    ctx.outboxBufferRef.current.clear()
    eventBus.emit('state_event_invalidate', {
      reason: 'backend-generation-change',
    })
  }
  window.sessionStorage.setItem(OUTBOX_GENERATION_KEY, ev.backend_generation)

  if (ev.sequence <= ctx.outboxAppliedRef.current) {
    return true // dedup
  }
  if (ev.sequence === ctx.outboxAppliedRef.current + 1) {
    ctx.outboxBufferRef.current.set(ev.sequence, ev)
    while (ctx.outboxBufferRef.current.has(ctx.outboxAppliedRef.current + 1)) {
      const next = ctx.outboxBufferRef.current.get(
        ctx.outboxAppliedRef.current + 1
      )!
      ctx.outboxBufferRef.current.delete(next.sequence)
      ctx.outboxAppliedRef.current = next.sequence
      window.sessionStorage.setItem(OUTBOX_CURSOR_KEY, String(next.sequence))
      eventBus.emit('state_event', next)
      eventBus.emit(`state_event:${next.entity_type}`, next)
      if (next.entity_type === 'runtime-status') {
        requestRuntimeStatusThrottled()
      }
    }
    try {
      send('state_event_ack', { cursor: ctx.outboxAppliedRef.current })
    } catch {
      // ack is retried by the next delivered event
    }
    return true
  }
  // Sequence gap → buffer bounded, invalidate, request resync.
  if (ctx.outboxBufferRef.current.size < OUTBOX_BUFFER_MAX) {
    ctx.outboxBufferRef.current.set(ev.sequence, ev)
  }
  eventBus.emit('state_event_invalidate', {
    reason: 'sequence-gap',
    expected: ctx.outboxAppliedRef.current + 1,
    received: ev.sequence,
  })
  try {
    send('state_event_resync', { cursor: ctx.outboxAppliedRef.current })
  } catch {
    // resync is retried on next event
  }
  return true
}
