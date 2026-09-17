export type FaultLike = {
  repair_outcome?: unknown
  raw_evidence?: unknown
}

export type PendingActionLike = {
  status?: unknown
  expires_at?: unknown
}

const ACTIVE_FAULT_OUTCOMES = new Set(['pending', 'failure', 'quarantined'])

const ACTIVE_ACTION_STATUSES = new Set([
  'awaiting-confirmation',
  'confirmed',
  'executing',
  'pending',
])

const TERMINAL_EVIDENCE_STATUSES = new Set([
  'expired',
  'invalidated',
  'executed',
  'failed',
  'completed',
  'cancelled',
  'canceled',
  'rolled-back',
  'rolled_back',
  'reverted',
  'revoked',
  'removed',
  'reconciled',
  'superseded',
  'resolved',
  'obsolete',
  'skipped',
  'success',
  'succeeded',
  'done',
  'closed',
  'rejected',
])

export function normalizeStatus(value: unknown): string {
  return String(value ?? '').trim().toLowerCase()
}

export function isPastExpiry(expiresAt: unknown, now: number = Date.now()): boolean {
  const raw = String(expiresAt ?? '').trim()
  if (!raw) return false
  const parsed = Date.parse(raw)
  return Number.isFinite(parsed) ? now > parsed : false
}

export function isActiveFault(fault: FaultLike | null | undefined): boolean {
  if (!fault || typeof fault !== 'object') return false
  const outcome = normalizeStatus(fault.repair_outcome)
  if (!ACTIVE_FAULT_OUTCOMES.has(outcome)) return false
  const evidence = fault.raw_evidence
  if (evidence && typeof evidence === 'object' && !Array.isArray(evidence)) {
    const evidenceStatus = normalizeStatus(
      (evidence as Record<string, unknown>).status
    )
    if (evidenceStatus && TERMINAL_EVIDENCE_STATUSES.has(evidenceStatus)) {
      return false
    }
  }
  return true
}

export function filterActiveFaults<T extends FaultLike>(
  faults: readonly T[] | null | undefined
): T[] {
  if (!Array.isArray(faults)) return []
  return faults.filter(isActiveFault)
}

export function isActivePendingAction(
  action: PendingActionLike | null | undefined
): boolean {
  if (!action || typeof action !== 'object') return false
  const status = normalizeStatus(action.status)
  if (status && !ACTIVE_ACTION_STATUSES.has(status)) return false
  return !isPastExpiry(action.expires_at)
}

export function filterActivePendingActions<T extends PendingActionLike>(
  actions: readonly T[] | null | undefined
): T[] {
  if (!Array.isArray(actions)) return []
  return actions.filter(isActivePendingAction)
}
