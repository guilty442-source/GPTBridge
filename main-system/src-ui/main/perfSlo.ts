import fs from 'node:fs'
import path from 'node:path'

/**
 * MS10 SLO 儀表資料層 — 讀取後端 `star-perf-baseline/v1` 快照
 * （`main-system/runtime/state/perf-baseline-latest.json`，由
 * `tasks/perf_baseline_job.py` 每 300 s 更新）並對照藍圖 SLO 預算評估。
 *
 * 量不到的欄位回 `null`／`available: false`，不造假（fail-closed）。
 */

// SLO 預算（藍圖 §4.2 資源表）：後端常駐 RSS ≤ 220 MB（30 min p95）、
// idle CPU ≤ 3%（單核當量）。快照值為即時取樣，budget 僅作參考線，
// 真正的 p95 驗收由 30 min 取樣稽核完成——UI 據此標示 measured。
export const SLO_BUDGETS = {
  backendRssMb: 220,
  idleCpuPercent: 3,
} as const

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

function asNumber(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null
}

function budgetStatus(value: number | null, budget: number | null): SloStatus {
  if (value === null) return 'unknown'
  if (budget === null) return 'measured'
  if (value >= budget) return 'over'
  if (value >= budget * 0.9) return 'warn'
  return 'ok'
}

function metric(
  key: string,
  value: number | null,
  unit: SloMetric['unit'],
  budget: number | null
): SloMetric {
  return { key, value, unit, budget, status: budgetStatus(value, budget) }
}

export function evaluateBaseline(
  snapshot: unknown,
  nowMs: number = Date.now()
): PerfSloReport {
  const root = asRecord(snapshot)
  const report: PerfSloReport = {
    available: false,
    baselineVersion: null,
    capturedAt: null,
    snapshotAgeS: null,
    metrics: [],
    ipcPerCommand: [],
  }
  if (!root) {
    report.error = 'snapshot-not-an-object'
    return report
  }
  report.baselineVersion =
    typeof root.baseline_version === 'string' ? root.baseline_version : null
  report.capturedAt =
    typeof root.captured_at === 'string' ? root.captured_at : null
  if (report.capturedAt) {
    const capturedMs = Date.parse(report.capturedAt)
    report.snapshotAgeS = Number.isFinite(capturedMs)
      ? Math.max(0, Math.round((nowMs - capturedMs) / 1000))
      : null
  }

  const proc = asRecord(root.process)
  const gpu = asRecord(root.gpu)
  const ipc = asRecord(root.ipc)

  const rssMb = proc ? asNumber(proc.rss_mb) : null
  const cpuPercent = proc ? asNumber(proc.cpu_percent) : null
  const gpuUsedMb = gpu ? asNumber(gpu.used_mb) : null
  const gpuTotalMb = gpu ? asNumber(gpu.total_mb) : null
  const gpuUtil = gpu ? asNumber(gpu.utilization) : null
  const ipcP50 = ipc ? asNumber(ipc.p50_ms) : null
  const ipcP95 = ipc ? asNumber(ipc.p95_ms) : null
  const ipcP99 = ipc ? asNumber(ipc.p99_ms) : null
  const ipcSamples = ipc ? asNumber(ipc.samples) : null

  report.metrics = [
    metric('ipc_p95_ms', ipcP95, 'ms', null),
    metric('ipc_p50_ms', ipcP50, 'ms', null),
    metric('ipc_p99_ms', ipcP99, 'ms', null),
    metric('backend_rss_mb', rssMb, 'MB', SLO_BUDGETS.backendRssMb),
    metric('cpu_percent', cpuPercent, 'percent', null),
    metric('gpu_vram_used_mb', gpuUsedMb, 'MB', gpuTotalMb),
    metric('gpu_utilization', gpuUtil, 'percent', null),
    metric('ipc_samples', ipcSamples, 'count', null),
  ]

  const perCommand = asRecord(ipc?.per_command)
  if (perCommand) {
    report.ipcPerCommand = Object.entries(perCommand)
      .map(([command, stats]) => {
        const row = asRecord(stats)
        return {
          command,
          samples: asNumber(row?.samples) ?? 0,
          p95_ms: asNumber(row?.p95_ms),
        }
      })
      .sort((a, b) => (b.p95_ms ?? 0) - (a.p95_ms ?? 0))
  }

  report.available = report.metrics.some((m) => m.value !== null)
  if (!report.available) report.error = 'no-measurable-fields'
  return report
}

export async function getPerfSlo(
  workspaceRoot: string,
  nowMs: number = Date.now()
): Promise<PerfSloReport> {
  const snapshotPath = path.join(
    workspaceRoot,
    'main-system',
    'runtime',
    'state',
    'perf-baseline-latest.json'
  )
  let raw: string
  try {
    raw = await fs.promises.readFile(snapshotPath, 'utf-8')
  } catch {
    return {
      available: false,
      baselineVersion: null,
      capturedAt: null,
      snapshotAgeS: null,
      metrics: [],
      ipcPerCommand: [],
      error: 'snapshot-unavailable',
    }
  }
  try {
    return evaluateBaseline(JSON.parse(raw), nowMs)
  } catch {
    return {
      available: false,
      baselineVersion: null,
      capturedAt: null,
      snapshotAgeS: null,
      metrics: [],
      ipcPerCommand: [],
      error: 'snapshot-malformed',
    }
  }
}
