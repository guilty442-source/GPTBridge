/**
 * Logging Governance: 實作結構化日誌
 */
export interface LogEntry {
  timestamp: string
  service: string
  level: 'info' | 'warn' | 'error' | 'debug'
  event: string
  detail: any
}

export const BootLogger = {
  log: (
    service: string,
    event: string,
    detail: any = {},
    level: LogEntry['level'] = 'info'
  ) => {
    const entry: LogEntry = {
      timestamp: new Date().toISOString(),
      service,
      event,
      detail,
      level,
    }
    console.log(`[${entry.service}] ${entry.event}`, entry)
  },
}
