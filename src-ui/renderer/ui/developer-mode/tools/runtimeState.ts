import { developerToolRegistry } from './registry'
import type { ToolAction, ToolRuntimeState } from './types'

export function createInitialToolRuntimeState(): ToolRuntimeState[] {
  const now = Date.now()
  return developerToolRegistry.map((tool) => ({
    ...tool,
    status: 'stopped',
    updatedAt: now,
    note: '未啟動',
  }))
}

export function resolveToolAction(
  tools: ToolRuntimeState[],
  toolId: string,
  action: ToolAction,
  phase: 'pending' | 'settled'
): ToolRuntimeState[] {
  const now = Date.now()
  return tools.map((tool) => {
    if (tool.id !== toolId) {
      return tool
    }

    if (phase === 'pending') {
      return {
        ...tool,
        status: action === 'start' ? 'starting' : 'stopping',
        updatedAt: now,
        note: action === 'start' ? '啟動中' : '停止中',
      }
    }

    return {
      ...tool,
      status: action === 'start' ? 'running' : 'stopped',
      updatedAt: now,
      note: action === 'start' ? '運作中' : '未啟動',
    }
  })
}
