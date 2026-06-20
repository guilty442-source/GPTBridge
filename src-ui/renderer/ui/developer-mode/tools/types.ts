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
  executablePath?: string
  executableExists?: boolean
  projectSizeBytes?: number
  description?: string
  has_custom_ui?: boolean
  hasCustomUi?: boolean
  hiddenFromToolbox?: boolean
  hidden_from_toolbox?: boolean
  mergedInto?: string
  merged_into?: string
  launchable?: boolean
  windowOnly?: boolean
}

export interface ToolRuntimeState extends ToolDefinition {
  status: ToolRuntimeStatus
  updatedAt: number
  note: string
}
