export type ToolRuntimeStatus =
  | 'stopped'
  | 'starting'
  | 'running'
  | 'stopping'
  | 'error'

export type ToolAction = 'start' | 'stop'

export interface ToolDefinition {
  id: string
  name: string
  summary: string
  folderPath?: string
  manifestPath?: string
  codePath?: string
  projectSizeBytes?: number
  launchable?: boolean
  windowOnly?: boolean
}

export interface ToolRuntimeState extends ToolDefinition {
  status: ToolRuntimeStatus
  updatedAt: number
  note: string
}
