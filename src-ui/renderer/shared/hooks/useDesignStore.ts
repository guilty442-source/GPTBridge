import { create } from 'zustand'

export interface ChildProjectState {
  toolName: string
  filePath: string
  content: string
  projectDir: string
  referenceType: string
}

interface DesignState {
  aiAnswers: string[]
  chatgptAnswers: string[]
  geminiAnswers: string[]
  designDiff: string
  childFile: ChildProjectState

  // Actions
  setAiAnswers: (updater: string[] | ((prev: string[]) => string[])) => void
  setChatgptAnswers: (
    updater: string[] | ((prev: string[]) => string[])
  ) => void
  setGeminiAnswers: (updater: string[] | ((prev: string[]) => string[])) => void
  setDesignDiff: (diff: string) => void
  setChildFile: (
    updater:
      | ChildProjectState
      | ((prev: ChildProjectState) => ChildProjectState)
  ) => void
}

export const useDesignStore = create<DesignState>((set) => ({
  aiAnswers: [],
  chatgptAnswers: [],
  geminiAnswers: [],
  designDiff: '',
  childFile: {
    toolName: 'ChildTool',
    filePath: 'main.py',
    content: '',
    projectDir: '',
    referenceType: 'python_desktop',
  },

  setAiAnswers: (updater) =>
    set((state) => ({
      aiAnswers:
        typeof updater === 'function' ? updater(state.aiAnswers) : updater,
    })),
  setChatgptAnswers: (updater) =>
    set((state) => ({
      chatgptAnswers:
        typeof updater === 'function' ? updater(state.chatgptAnswers) : updater,
    })),
  setGeminiAnswers: (updater) =>
    set((state) => ({
      geminiAnswers:
        typeof updater === 'function' ? updater(state.geminiAnswers) : updater,
    })),
  setDesignDiff: (designDiff) => set({ designDiff }),
  setChildFile: (updater) =>
    set((state) => ({
      childFile:
        typeof updater === 'function' ? updater(state.childFile) : updater,
    })),
}))
