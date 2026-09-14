import { useSyncExternalStore } from 'react'
import type { RuntimeStatusPayload } from '@/ui/sovereign/runtimeStatusTypes'
import {
  getRuntimeStatusState,
  subscribeRuntimeStatus,
} from '../services/runtimeStatusStore'

/**
 * Subscribe to one runtime-status field.  The component re-renders only
 * when that field changes —modular refresh, never a whole-UI update.
 */
export function useRuntimeStatusField<K extends keyof RuntimeStatusPayload>(
  key: K
): RuntimeStatusPayload[K] {
  return useSyncExternalStore(
    (listener) => subscribeRuntimeStatus(key, listener),
    () => getRuntimeStatusState()[key]
  )
}
