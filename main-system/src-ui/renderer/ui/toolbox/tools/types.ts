export type ToolRuntimeStatus =
  | 'stopped'
  | 'starting'
  | 'running'
  | 'stopping'
  | 'error'

export type ToolAction = 'start' | 'stop'

export type ToolRuntimeMode =
  | 'executable'
  | 'governed-source'
  | 'dual-runtime'

export interface ToolDataBoundary {
  standalone: boolean
  codeScope?: string
  databaseScope?: string
}

export interface ToolCapacityCategory {
  sizeBytes: number
  fileCount: number
}

export interface ToolCapacityBreakdown {
  program: ToolCapacityCategory
  runtime: ToolCapacityCategory
  userData: ToolCapacityCategory
  cache: ToolCapacityCategory
  backups: ToolCapacityCategory
}

export interface ToolDefinition {
  id: string
  name: string
  summary: string
  folderPath?: string
  manifestPath?: string
  codePath?: string
  executablePath?: string
  executableExists?: boolean
  runtimeAvailable?: boolean
  runtimeMode?: ToolRuntimeMode
  automaticRuntimeMode?: Exclude<ToolRuntimeMode, 'dual-runtime'>
  projectSizeBytes?: number
  projectFileCount?: number
  capacityBreakdown?: ToolCapacityBreakdown
  dataBoundary?: ToolDataBoundary
  description?: string
  has_custom_ui?: boolean
  hasCustomUi?: boolean
  hiddenFromToolbox?: boolean
  hidden_from_toolbox?: boolean
  mergedInto?: string
  merged_into?: string
  launchable?: boolean
  lifecycleLocked?: boolean
  windowOnly?: boolean
}

export interface ToolRuntimeState extends ToolDefinition {
  status: ToolRuntimeStatus
  updatedAt: number
  note: string
}
