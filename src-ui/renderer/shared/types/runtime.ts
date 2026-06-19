export type ServiceStatus =
  | 'INIT'
  | 'SUCCESS'
  | 'FAIL'
  | 'TIMEOUT'
  | 'DEGRADED'
  | 'SKIP'
  | 'BOOTING'

export interface RuntimeService {
  id?: string
  name: string
  status: ServiceStatus
  elapsed?: number
  error?: string
}

export type ToolStatus =
  | 'stopped'
  | 'starting'
  | 'running'
  | 'stopping'
  | 'error'

export interface Tool {
  id: string
  name: string
  status: ToolStatus
}
