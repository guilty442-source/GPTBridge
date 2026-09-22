/**
 * `app:get-perf-slo` IPC payload — mirrors `src-ui/main/perfSlo.ts`.
 * The IPC boundary returns `unknown`; this is the typed projection.
 */
export type SloStatus = 'ok' | 'warn' | 'over' | 'measured' | 'unknown'

export interface SloMetric {
  key: string
  value: number | null
  unit: 'ms' | 'MB' | 'percent' | 'count'
  budget: number | null
  status: SloStatus
}

export interface SloIpcCommand {
  command: string
  samples: number
  p95_ms: number | null
}

export interface PerfSloReport {
  available: boolean
  baselineVersion: string | null
  capturedAt: string | null
  snapshotAgeS: number | null
  metrics: SloMetric[]
  ipcPerCommand: SloIpcCommand[]
  error?: string
}
