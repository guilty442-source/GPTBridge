import os from 'node:os'
import fs from 'node:fs'
import path from 'node:path'

import { getRuntimeEnv } from './runtime-env'

let lastCpuSnapshot: { idle: number; total: number } | null = null

export function readCpuSnapshot(): { idle: number; total: number } {
  let idle = 0
  let total = 0
  for (const cpu of os.cpus()) {
    idle += cpu.times.idle
    total +=
      cpu.times.user +
      cpu.times.nice +
      cpu.times.sys +
      cpu.times.irq +
      cpu.times.idle
  }
  return { idle, total }
}

export function readCpuUsagePercent(): number | null {
  const current = readCpuSnapshot()
  if (!lastCpuSnapshot) {
    lastCpuSnapshot = current
    return null
  }

  const totalDiff = current.total - lastCpuSnapshot.total
  const idleDiff = current.idle - lastCpuSnapshot.idle
  lastCpuSnapshot = current

  if (totalDiff <= 0) return null
  const usage = (1 - idleDiff / totalDiff) * 100
  return Math.max(0, Math.min(100, usage))
}

export function readDiskMetrics(
  rootPath: string
): { totalBytes: number; freeBytes: number; usagePercent: number } | null {
  try {
    const stats = fs.statfsSync(rootPath)
    const blockSize = Number((stats as any).bsize ?? 0)
    const totalBlocks = Number((stats as any).blocks ?? 0)
    const freeBlocks = Number(
      (stats as any).bavail ?? (stats as any).bfree ?? 0
    )

    if (blockSize <= 0 || totalBlocks <= 0) return null

    const totalBytes = blockSize * totalBlocks
    const freeBytes = blockSize * freeBlocks
    const usagePercent = ((totalBytes - freeBytes) / totalBytes) * 100

    return {
      totalBytes,
      freeBytes,
      usagePercent: Math.max(0, Math.min(100, usagePercent)),
    }
  } catch {
    return null
  }
}

export function resolveSystemDiskRoot(): string {
  if (process.platform !== 'win32') return path.parse(os.homedir()).root || '/'

  const configuredDrive = (getRuntimeEnv('SystemDrive') || '').trim()
  if (/^[a-z]:$/i.test(configuredDrive)) return `${configuredDrive}\\`
  if (configuredDrive) return path.parse(path.resolve(configuredDrive)).root
  return path.parse(os.homedir()).root || path.parse(process.cwd()).root
}

export function getSystemMetrics() {
  const totalMemBytes = os.totalmem()
  const freeMemBytes = os.freemem()
  const ramUsagePercent =
    totalMemBytes > 0
      ? ((totalMemBytes - freeMemBytes) / totalMemBytes) * 100
      : 0

  const diskRoot = resolveSystemDiskRoot()
  const disk = readDiskMetrics(diskRoot)

  return {
    cpuUsagePercent: readCpuUsagePercent(),
    ramUsagePercent: Math.max(0, Math.min(100, ramUsagePercent)),
    ramTotalBytes: totalMemBytes,
    ramFreeBytes: freeMemBytes,
    diskUsagePercent: disk?.usagePercent ?? null,
    diskTotalBytes: disk?.totalBytes ?? null,
    diskFreeBytes: disk?.freeBytes ?? null,
    diskRoot,
    sampledAt: Date.now(),
  }
}
