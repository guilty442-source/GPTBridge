import type { ToolDefinition } from '@/ui/developer-mode/tools/types'

/**
 * Application registry keeps standalone window entries and child applications.
 * Child runtime implementations live under platform_tools/<tool-id>/.
 */
export const toolboxToolRegistry: ToolDefinition[] = [
  {
    id: 'ai-assistant',
    name: 'AI投資管家',
    summary: '投資資料、AI 群組推理、共享記憶與任務看板。',
    folderPath: 'platform_tools/ai-assistant',
    launchable: true,
    windowOnly: true,
  },
  {
    id: 'project-cleaner',
    name: '清理工具',
    summary: '清理專案垃圾、快取與臨時檔。',
    folderPath: 'platform_tools/project-cleaner',
    launchable: true,
    windowOnly: true,
  },
  {
    id: 'agent-coder',
    name: '系統救援工具',
    summary: '管理應用程式程式碼、修補指令與單元測試。',
    folderPath: 'platform_tools/agent-coder',
    launchable: true,
    windowOnly: true,
  },
  {
    id: 'vaultly',
    name: '影音下載自動化',
    summary: '集中模組：platform_tools/vaultly。',
    folderPath: 'platform_tools/vaultly',
    launchable: true,
  },
  {
    id: 'file-sorter',
    name: '自動化檔案管理',
    summary: '自動分類、關鍵字規則、媒體問題掃描與相似影片偵測整合工具。',
    folderPath: 'platform_tools/file-sorter',
    launchable: true,
  },
]
