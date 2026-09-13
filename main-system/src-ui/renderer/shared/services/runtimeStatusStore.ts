/**
 * Runtime status store — modular, per-field subscriptions.
 *
 * The backend owns the refresh and pushes compact health reports.  Reports
 * are applied here field by field; only subscribers of a field that
 * actually changed are notified, so modules (Xingcheng panel, connection
 * indicator, capacity, sovereign dashboard) refresh independently instead
 * of re-rendering the whole UI.
 */
import type { RuntimeStatusPayload } from '@/ui/sovereign/SovereignDashboard'

type RuntimeStatusKey = keyof RuntimeStatusPayload
type Listener = () => void

let state: RuntimeStatusPayload = {}
const listeners = new Map<RuntimeStatusKey, Set<Listener>>()

export function getRuntimeStatusState(): RuntimeStatusPayload {
  return state
}

export function subscribeRuntimeStatus(
  key: RuntimeStatusKey,
  listener: Listener
): () => void {
  let bucket = listeners.get(key)
  if (!bucket) {
    bucket = new Set()
    listeners.set(key, bucket)
  }
  bucket.add(listener)
  return () => {
    bucket?.delete(listener)
  }
}

function isEqual(a: unknown, b: unknown): boolean {
  // Reports replace values wholesale; reference/primitive comparison is
  // O(1) and never blocks the UI (no deep serialization on hot paths).
  return Object.is(a, b)
}

/**
 * Apply a partial report.  Fields that did not change are ignored, and
 * listeners are notified only for fields that changed — this is what keeps
 * the refresh modular instead of a whole-UI update.
 */
export function applyRuntimeStatusReport(
  report: Partial<RuntimeStatusPayload> | null | undefined
): void {
  if (!report || typeof report !== 'object') return
  const changed: RuntimeStatusKey[] = []
  const next: RuntimeStatusPayload = { ...state }
  for (const key of Object.keys(report) as RuntimeStatusKey[]) {
    const value = report[key]
    if (value === undefined) continue
    if (isEqual(state[key], value)) continue
    // @ts-expect-error indexed assignment across the payload union
    next[key] = value
    changed.push(key)
  }
  if (changed.length === 0) return
  state = next
  for (const key of changed) {
    const bucket = listeners.get(key)
    if (!bucket) continue
    for (const listener of Array.from(bucket)) {
      try {
        listener()
      } catch {
        // A failing subscriber must never break other modules.
      }
    }
  }
}

export function resetRuntimeStatusStore(): void {
  state = {}
}
