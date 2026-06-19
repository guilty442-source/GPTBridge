import type { LogGroups, TaskProgress } from '@/types/ui'
import { create } from 'zustand'

interface WorkflowState {
  logGroups: LogGroups
  taskProgress: Record<string, TaskProgress>
  recentEvents: string[]
  status: string
  lastError: string
  lastStatusAt: number | null

  // Actions (支援像 React useState 一樣傳入 function)
  setLogGroups: (updater: LogGroups | ((prev: LogGroups) => LogGroups)) => void
  setTaskProgress: (
    updater:
      | Record<string, TaskProgress>
      | ((prev: Record<string, TaskProgress>) => Record<string, TaskProgress>)
  ) => void
  setRecentEvents: (updater: string[] | ((prev: string[]) => string[])) => void
  setStatus: (status: string) => void
  setLastError: (error: string) => void
  setLastStatusAt: (time: number | null) => void
}

export const useWorkflowStore = create<WorkflowState>((set) => ({
  logGroups: { core: [], design: [], developer: [] },
  taskProgress: {},
  recentEvents: [],
  status: 'Disconnected',
  lastError: '',
  lastStatusAt: null,

  setLogGroups: (updater) =>
    set((state) => ({
      logGroups:
        typeof updater === 'function' ? updater(state.logGroups) : updater,
    })),
  setTaskProgress: (updater) =>
    set((state) => ({
      taskProgress:
        typeof updater === 'function' ? updater(state.taskProgress) : updater,
    })),
  setRecentEvents: (updater) =>
    set((state) => ({
      recentEvents:
        typeof updater === 'function' ? updater(state.recentEvents) : updater,
    })),
  setStatus: (status) => set({ status }),
  setLastError: (lastError) => set({ lastError }),
  setLastStatusAt: (lastStatusAt) => set({ lastStatusAt }),
}))
